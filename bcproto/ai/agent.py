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



