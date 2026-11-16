import asyncio
import hashlib
import random
import secrets
import sys

# 匯入放在bcproto/wire.py的A1(encode)跟A2(decode_line)
try:
    from bcproto.wire import decode_line, encode
    from bcproto.ai.stratagy import choose_guess
    from bcproto.rules import ALL_NUMBERS, filter_candidates, score
except ImportError:
    from wire import decode_line, encode
    from stratagy import choose_guess
    from rules import ALL_NUMBERS, filter_candidates, score

class AIAgent:
    def __init__(self, host: str, port: int, name: str = "G09-AI", team: str = "G09", quiet: bool = False):
        self.host = host
        self.port = port
        self.name = name
        self.team = team
        self.quiet = quiet
        self.writer: asyncio.StreamWriter | None = None
        # C2: 紀錄server指派的player_id
        self.player_id: str | None = None
        # C4: 儲存自己這局的四位數跟鹽值
        self.secret: str = ""
        self.salt: str = ""
        self.feedback_turns: set[int] = set()
        self.reveal_sent = False
        self.candidates: dict[str, list[str]] = {}

    async def send_msg(self, msg: dict):
        """將 dict 封裝為 JSON Lines 並透過 TCP 發送 (使用 A1 encode)"""
        if not self.writer:
            return
        raw_bytes = encode(msg)
        self.writer.write(raw_bytes)
        await self.writer.drain()

    async def send_hello(self):
        """C2 核心動作：發送 HELLO 握手註冊封包"""
        hello_msg = {
            "type": "HELLO",
            "protocol": "BC/1.0",
            "name": self.name,
            "team": self.team,
            "kind": "ai",
        }
        if not self.quiet:
            print(f"[C2 握手] 發送 HELLO: name={self.name}, team={self.team}")
        await self.send_msg(hello_msg)

    async def handle_wellcome(self, msg: dict):
        """C2 核心動作：處理伺服器回傳的 WELCOME 封包"""
        self.player_id = msg.get("player_id")
        protocol = msg.get("protocol")
        if not self.quiet:
            print(f"[C2 握手成功] 已加入大廳！分配到的 player_id: {self.player_id} (協定: {protocol})")

    # C3：處理PING自動回PONG
    async def handle_ping(self, msg: dict):
        """C3:收到 PING，回覆帶相同nonce的 PONG"""
        pong_msg = {"type": "PONG"}
        if "nonce" in msg:
            pong_msg["nonce"] = msg["nonce"]

        if not self.quiet:
            print(f"[C3 心跳] 收到 PING (nonce={msg.get('nonce')}) -> 回覆 PONG")
        await self.send_msg(pong_msg)

    # C4：處以GAME_START，產生secret跟salt送出commit
    def generate_secret(self) -> str:
        """隨機生成 4 位不重複數字"""
        digits = "0123456789"
        rng = secrets.SystemRandom()
        return "".join(rng.sample(digits, 4))

    def calculate_commit(self, secret: str, salt: str) -> str:
        """計算 SHA-256(secret + ":" + salt)"""
        raw = f"{secret}:{salt}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def handle_game_start(self, msg: dict):
        """C4：產生 secret/salt，送出COMMIT"""
        self.feedback_turns.clear()
        self.reveal_sent = False
        # 正式協定的 players 是 {"id": ...} 物件；舊版 mock server
        # 可能只傳 player_id 字串，兩種格式都接受。
        player_ids = [
            player["id"] if isinstance(player, dict) else player
            for player in msg.get("players", [])
        ]
        self.candidates = {
            player_id: list(ALL_NUMBERS)
            for player_id in player_ids
            if player_id != self.player_id
        }
        self.secret = self.generate_secret()
        self.salt = secrets.token_hex(16)
        commit_hash = self.calculate_commit(self.secret, self.salt)

        if not self.quiet:
            print(f"[C4 開局] 遊戲開始！")
            print(
                f"[C4 產生秘密] secret={self.secret}, salt={self.salt[:8]}...,"
                f" commit={commit_hash[:10]}..."
            )

        commit_msg = {"type": "COMMIT", "commit": commit_hash}
        await self.send_msg(commit_msg)

    async def handle_turn(self, msg: dict):
        """輪到自己時，回覆同一回合的 GUESS。"""
        if msg.get("guesser") != self.player_id:
            return

        turn_id = msg.get("turn_id")
        value = choose_guess()
        guess_msg = {"type": "GUESS", "turn_id": turn_id, "guess": value}

        if not self.quiet:
            print(f"[C5 喊數] 輪到自己 (turn_id={turn_id}) -> GUESS {value}")
        await self.send_msg(guess_msg)

    async def handle_guess_made(self, msg: dict):
        """自己是回報者時，計算秘密數字的 (A, B) 並回傳 FEEDBACK。"""
        if self.player_id not in msg.get("responders", []):
            return

        turn_id = msg.get("turn_id")
        if turn_id in self.feedback_turns:
            return

        guess_value = msg.get("guess")
        a, b = score(guess_value, self.secret)
        self.feedback_turns.add(turn_id)
        feedback_msg = {"type": "FEEDBACK", "turn_id": turn_id, "a": a, "b": b}

        if not self.quiet:
            print(f"[C5 回報] turn_id={turn_id} guess={guess_value} -> {a}A{b}B")
        await self.send_msg(feedback_msg)

    async def handle_reveal_request(self, msg: dict):
        """收到揭示要求時，自動送出本局的秘密與鹽值。"""
        if self.reveal_sent or not self.secret or not self.salt:
            return

        self.reveal_sent = True
        reveal_msg = {"type": "REVEAL", "secret": self.secret, "salt": self.salt}

        if not self.quiet:
            print(f"[C6 揭示] 收到 REVEAL_REQUEST ({msg.get('reason')}) -> 送出秘密")
        await self.send_msg(reveal_msg)

    async def handle_turn_result(self, msg: dict):
        """用每一則 TURN_RESULT 的回報更新所有對手候選集合。"""
        guess_value = msg.get("guess")
        if guess_value is None:
            return

        for result in msg.get("results", []):
            # 正式協定欄位是 player；舊版 mock server 曾使用 player_id。
            player_id = result.get("player", result.get("player_id"))
            if player_id not in self.candidates:
                continue
            self.candidates[player_id] = filter_candidates(
                self.candidates[player_id],
                guess_value,
                result["a"],
                result["b"],
            )

        if not self.quiet:
            sizes = ", ".join(
                f"{player_id}={len(cands)}"
                for player_id, cands in self.candidates.items()
            )
            print(f"[C5 推理] turn_id={msg.get('turn_id')} 候選數: {sizes}")

    async def dispatch(self, msg: dict):
        """訊息分派中心"""
        mtype = msg.get("type")

        if mtype == "WELCOME":
            await self.handle_wellcome(msg)
        elif mtype == "PING":
            await self.handle_ping(msg)
        elif mtype == "GAME_START":
            await self.handle_game_start(msg)
        elif mtype == "TURN":
            await self.handle_turn(msg)
        elif mtype == "GUESS_MADE":
            await self.handle_guess_made(msg)
        elif mtype == "REVEAL_REQUEST":
            await self.handle_reveal_request(msg)
        elif mtype == "TURN_RESULT":
            await self.handle_turn_result(msg)
        elif mtype == "ERROR":
            print(f"[伺服器錯誤] code={msg.get('code')}, message={msg.get('message')}")
        else:
            if not self.quiet:
                print(f"[C1 接收] 收到未處理類型: {mtype} | 內容: {msg}")

    async def run(self):
        """TCP連線跟StreamReader讀取迴圈"""
        print(f"[C1] 嘗試連線至 {self.host}:{self.port} ...")
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port, limit=4095,)
            self.writer = writer
            print(f"[C1] 連線成功！進入封包監聽迴圈...")
        except Exception as e:
            print(f"[C1 錯誤] 無法連線至伺服器: {e}")
            return

        try:
            #C2: 連線成功後，先發送HELLO封包
            await self.send_hello()

            while True:
                line = await reader.readline()
                if not line:
                    print("[C1] 伺服器已關閉連線。")
                    break
                # 用A2(decode_line)解析收到的訊息
                msg, err = decode_line(line)
                if err:
                    print(f"[C1 警告] 收到無法解析的訊息，錯誤碼: {err}")
                    continue
                if msg is None:
                    # 忽略空行
                    continue
                # 把dict丟給dispatch
                await self.dispatch(msg)
        except asyncio.CancelledError:
            pass
        except ValueError as e:
            print(f"[C1 警告] 收到超長訊息，連線關閉：{e}")
        finally:
            writer.close()
            await writer.wait_closed()
            print("[C1] 連線已安全關閉")



# ---------------- TURN_RESULT 獨立驗收測試 ----------------
def _score_pair(guess: str, secret: str) -> tuple[int, int]:
    """測試腳本內建標準算分器"""
    a = sum(1 for g, s in zip(guess, secret) if g == s)
    b = sum(1 for g in guess if g in secret) - a
    return a, b

async def mock_server(reader, writer):
    # 1. [C2] 握手發放 ID: p-test-01
    await reader.readline()
    writer.write(b'{"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p-test-01"}\n')
    await writer.drain()

    # 2. [C4] 廣播開局（3位玩家：自己 + 2位對手），AI 應該要為 2 位對手各初始化 5040 個候選數
    await asyncio.sleep(0.2)
    print("\n[Mock Server] 廣播 GAME_START (玩家: p-test-01, p-other-02, p-other-03)...")
    writer.write(b'{"type": "GAME_START", "players": ["p-test-01", "p-other-02", "p-other-03"]}\n')
    await writer.drain()
    await reader.readline()  # 讀掉 COMMIT

    # 3. 模擬第 1 輪結算 (TURN_RESULT)：你猜了 "1234"
    # 假設 p-other-02 的秘密是 "1289" -> 回報 2A0B
    # 假設 p-other-03 的秘密是 "5678" -> 回報 0A0B
    await asyncio.sleep(0.3)
    print("[Mock Server] 廣播第 1 輪 TURN_RESULT：p-test-01 猜 '1234' (p-other-02: 2A0B, p-other-03: 0A0B)")
    turn1_msg = (
        b'{"type": "TURN_RESULT", "turn_id": 1, "guesser": "p-test-01", "guess": "1234", '
        b'"results": ['
        b'{"player_id": "p-other-02", "a": 2, "b": 0}, '
        b'{"player_id": "p-other-03", "a": 0, "b": 0}'
        b']}\n'
    )
    writer.write(turn1_msg)
    await writer.drain()

    # 4. 模擬第 2 輪結算 (TURN_RESULT)：p-other-02 猜了 "1289"
    # p-test-01 (你自己) 也回報了，但 AI 不應該把自己當對手建立候選集
    # p-other-03 回報 0A2B (假設秘密 "5678" 對上 "1289" -> 8在位置3對1個? 不對，測試純看過濾邏輯)
    await asyncio.sleep(0.3)
    print("[Mock Server] 廣播第 2 輪 TURN_RESULT：p-other-02 猜 '1289' (p-test-01: 1A1B, p-other-03: 0A1B)")
    turn2_msg = (
        b'{"type": "TURN_RESULT", "turn_id": 2, "guesser": "p-other-02", "guess": "1289", '
        b'"results": ['
        b'{"player_id": "p-test-01", "a": 1, "b": 1}, '
        b'{"player_id": "p-other-03", "a": 0, "b": 1}'
        b']}\n'
    )
    writer.write(turn2_msg)
    await writer.drain()

    await asyncio.sleep(0.3)

    # 5. 驗收你的 AI 內部候選集合狀態（預設名稱為 agent_instance.candidates，若你取別的名字可自行調整）
    candidates_map = getattr(agent_instance, "candidates", None)
    if not isinstance(candidates_map, dict):
        print("[驗收失敗] 找不到 dict 型態的 self.candidates 屬性！")
    else:
        print(f"\n[驗收檢查] 目前追蹤的對手清單: {list(candidates_map.keys())}")
        
        # 檢查 A：不能把自己放進對手候選集
        has_self = "p-test-01" in candidates_map
        
        # 檢查 B：計算標準過濾後的剩餘數量
        all_nums = [f"{a}{b}{c}{d}" for a in "0123456789" for b in "0123456789" for c in "0123456789" for d in "0123456789" if len({a,b,c,d}) == 4]
        exp_p2 = [n for n in all_nums if _score_pair("1234", n) == (2, 0)]
        exp_p3 = [n for n in all_nums if _score_pair("1234", n) == (0, 0) and _score_pair("1289", n) == (0, 1)]

        actual_p2 = candidates_map.get("p-other-02", [])
        actual_p3 = candidates_map.get("p-other-03", [])

        if (not has_self and 
            len(actual_p2) == len(exp_p2) and set(actual_p2) == set(exp_p2) and
            len(actual_p3) == len(exp_p3) and set(actual_p3) == set(exp_p3)):
            print(f"[驗收通過] 完美！已排除自己，且 p-other-02 剩餘 {len(actual_p2)} 組、p-other-03 剩餘 {len(actual_p3)} 組候選數，與標準答案 100% 吻合！")
        else:
            print(f"[驗收失敗] 候選集合數量或內容不符！")
            print(f"  - 是否誤把自己加入對手: {has_self}")
            print(f"  - p-other-02 預期 {len(exp_p2)} 組，實際 {len(actual_p2)} 組")
            print(f"  - p-other-03 預期 {len(exp_p3)} 組，實際 {len(actual_p3)} 組")

    writer.close()
    await writer.wait_closed()

agent_instance: AIAgent

async def main():
    global agent_instance
    server = await asyncio.start_server(mock_server, "127.0.0.1", 8888)
    async with server:
        agent_instance = AIAgent("127.0.0.1", 8888, name="G09-AI")
        await agent_instance.run()

if __name__ == "__main__":
    asyncio.run(main())
