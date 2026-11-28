"""B8/B9/B16：遊戲引擎的基礎設施。**這個檔案由彭明山一個人改。**

目前完成的是不依賴協定層的三塊：

* **B8** `broadcast()`、`broadcast_lobby()`、`audience()`——廣播對象的判定規則
  （§11.3）只寫在這裡一次，協定層的 `on_chat` 直接呼叫 `broadcast()` 就好，
  不必自己判斷誰該收。
* **B9** `wait_until()`——把「協定層改欄位 + 敲鐘」轉成遊戲迴圈的 `await`。
* **B16** `log()` / `log_raw()` / `open_log()` / `close_log()`——§5.6 的
  JSON Lines 日誌，一局一檔，每筆立即 flush。

* **B10–B15** `run_game()` 的五階段流程：大廳、承諾、回合循環、一致性檢查、
  淘汰與終局揭示、名次。

階段之間用 `self.phase` 交棒。協定層的 `check_message()` 讀的就是這個欄位，
所以換階段等於同時換掉「現在接受哪些訊息」——這是兩層之間唯一的約定。
"""

import asyncio
import json
import os
import random
import sys
import threading
import time
import uuid

from ..rules import compute_ranking, filter_candidates, verify_reveal

#: `LOBBY.min_players` 固定為 2（§11.2）；`max_players` 才是 `--players`。
MIN_PLAYERS = 2


class GameMixin:
    """大廳、回合循環、揭示、名次與日誌。狀態欄位一律在 `Server.__init__`。"""

    # ==================================================================
    # B9：把「改欄位 + changed.set()」轉成 await
    # ==================================================================

    async def wait_until(self, cond, timeout: float | None = None) -> bool:
        """等 `cond()` 為真，或等到逾時。

        協定層收到合法訊息後會改欄位再呼叫 `changed.set()`，我們被喚醒、
        重新評估 `cond()`。回傳 `True` 表示條件成立，`False` 表示逾時。

        `timeout` 為 `None` 時無限等待（大廳等人就是這種）。所有時限選項都必須
        接受小數秒數（§5.2），所以這裡一律當 float 用。

        實作上有一個必須小心的競態：`changed` 是共用的 Event，我們得先
        `clear()` 再 `await wait()`，否則會被上一次的訊號立刻喚醒而空轉。
        但 `clear()` 與 `await` 之間協定層可能剛好 `set()`，那個訊號就被我們
        自己清掉了。**所以 `clear()` 之後必須再檢查一次 `cond()`**——這一行就是
        防止整局卡死 60 秒的關鍵。
        """
        if cond():
            return True

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            self.changed.clear()

            # clear() 可能清掉了剛剛才來的訊號，所以重新檢查一次。
            if cond():
                return True

            if deadline is None:
                remaining = None
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False

            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                # 逾時的瞬間條件也可能剛好成立，再確認一次才回報逾時。
                return cond()

            if cond():
                return True

    # ==================================================================
    # B8：廣播
    # ==================================================================

    def audience(self) -> list:
        """本次廣播該送給誰（§11.3）。

        對象是**所有已完成 `HELLO` 且仍連線**的玩家，**包括已被猜中、棄權、
        被取消資格但仍在線上的人**——他們要看得到後續戰況與終局名次。
        只有在承諾階段因未承諾而被移出本局的玩家（`status == "REMOVED"`）
        不在其內。

        這個判斷只寫在這裡一次，協定層不用自己重複一遍。
        """
        return [p for p in self.players.values()
                if p.joined and p.conn is not None and not p.conn.closed]

    def broadcast(self, msg: dict) -> None:
        """把一則訊息送給 `audience()` 裡的每個人。

        簽名固定為 `broadcast(msg: dict) -> None`：協定層的 `on_chat` 也會呼叫。
        逐一送出，單一連線送不出去不影響其他人（§5.3-1）。
        日誌由 `conn.send()` 自己寫，這裡不重複記錄。
        """
        for p in self.audience():
            p.conn.send(msg)

    def broadcast_lobby(self) -> None:
        """大廳成員變動時廣播 `LOBBY`（§11.2）。

        大廳清單**依加入順序**排列（終局名次才依名次、對局中才依輪轉順序）。
        `self.players` 是 dict，插入順序就是加入順序，直接走訪即可。
        """
        self.broadcast({
            "type": "LOBBY",
            "players": [{"id": p.pid, "name": p.name,
                         "team": p.team, "kind": p.kind}
                        for p in self.players.values() if p.joined],
            "min_players": MIN_PLAYERS,
            "max_players": self.a.players,
        })

    def close_all_conns(self) -> None:
        """一局結束後關閉本局所有連線（§5.3-6），含未完成 `HELLO` 的。"""
        for conn in list(self.conns):
            conn.close()

    # ==================================================================
    # B16：§5.6 的 JSON Lines 日誌
    # ==================================================================

    @staticmethod
    def new_game_id() -> str:
        """本局識別碼。需符合 §9 的 ``[A-Za-z0-9_-]{1,64}``。"""
        return uuid.uuid4().hex[:12]

    def open_log(self, game_id: str) -> None:
        """為這一局開一個日誌檔。

        規範要求**一局恰好一個檔案**、檔名包含 `game_id`、UTF-8 編碼，且
        **日誌目錄中只能有伺服器的日誌**（客戶端的日誌要寫到別的地方）。

        在大廳一開放就呼叫，不是等開局才呼叫——`HELLO` 與 `WELCOME` 也是
        「本局收送的訊息」，必須記進去。
        """
        self.close_log()
        os.makedirs(self.a.log_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.a.log_dir, f"{stamp}-{game_id}.jsonl")
        self.logf = open(path, "w", encoding="utf-8")

    def log(self, direction: str, peer: str, msg: dict) -> None:
        """記一則收送的訊息。

        欄位名稱與結構必須完全一致（§5.6）：`ts`（Unix 秒，可含小數）、
        `dir`（`send` / `recv`）、`peer`（對方 `player_id`，完成 `HELLO` 前
        寫 `IP:埠號`）、`msg`（訊息物件本身）。

        每筆寫入後**必須立即 flush**——助教會在對局進行中讀這個檔，
        而且行程若被強制結束，沒 flush 的內容會整段消失。
        """
        if self.logf is None:
            return
        record = {"ts": round(time.time(), 3), "dir": direction,
                  "peer": peer, "msg": msg}
        self.logf.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.logf.flush()

    def log_raw(self, direction: str, peer: str, text: str) -> None:
        """記一則無法解析的輸入：`msg` 欄位寫成 `{"raw": "…"}`（§5.6）。"""
        self.log(direction, peer, {"raw": text})

    def close_log(self) -> None:
        if self.logf is not None:
            self.logf.close()
            self.logf = None

    # ==================================================================
    # 小工具：誰還在、誰走了
    # ==================================================================

    def joined_players(self) -> list:
        """本局仍算數的玩家（承諾階段被移出的不算）。"""
        return [p for p in self.players.values() if p.joined]

    def alive_ids(self, order: list) -> list:
        """依輪轉順序排列的存活者 `pid`。`TURN.alive` 要的就是這個。"""
        return [pid for pid in order if self.players[pid].status == "ALIVE"]

    def gone(self, pid: str) -> bool:
        """連線已斷——等他回應沒有意義，條件要提早放行。"""
        conn = self.players[pid].conn
        return conn is None or conn.closed

    def depart(self, player, status: str) -> None:
        """讓一位玩家離場，記下離場回合。

        `out_turn` 必須有值，否則 §11.3 的 `GAME_OVER` 檢查會擋下來——
        只有 `SURVIVOR` 的 `out_turn` 可以是 null。
        """
        player.status = status
        if player.out_turn is None:
            player.out_turn = self.turn_id

    # ==================================================================
    # B10：LOBBY
    # ==================================================================

    async def lobby_phase(self) -> bool:
        """等人到齊。回傳 False 表示人數不足以開局。

        離開條件有兩個（§11.2、§5.3）：人數達 `--players` 自動開局，或莊家在
        主控台輸入 `start` 且至少 2 人。這裡無限等待，不設時限——大廳本來就
        沒有時限，而 `wait_until(cond, None)` 正好表達這件事。
        """
        self.phase = "LOBBY"
        self.broadcast_lobby()

        def ready():
            joined = len(self.joined_players())
            return joined >= self.a.players or (self.manual_start and joined >= MIN_PLAYERS)

        await self.wait_until(ready, None)
        return len(self.joined_players()) >= MIN_PLAYERS

    # ==================================================================
    # B11：COMMIT
    # ==================================================================

    async def commit_phase(self) -> list | None:
        """送 `GAME_START`、等承諾、移出未承諾者、送 `COMMITS`。

        回傳輪轉順序；人數不足以繼續時回傳 None（由 `run_game` 發 `ABORTED`）。

        輪轉順序**隨機**決定，不能用加入順序——先連進來的人若總是先喊數，
        就等於多拿一次資訊優勢。`GAME_START.players` 的排列必須與 `order`
        一致（§11.2 會檢查），所以兩者一起產生。
        """
        self.phase = "COMMIT"
        players = self.joined_players()
        for p in players:
            p.status = "ALIVE"          # 協定層要 ALIVE 才收 COMMIT
        random.shuffle(players)

        self.broadcast({
            "type": "GAME_START",
            "game_id": self.game_id,
            "players": [{"id": p.pid, "name": p.name, "team": p.team, "kind": p.kind}
                        for p in players],
            "order": [p.pid for p in players],
            "rules": {
                "guess_timeout": self.a.guess_timeout,
                "feedback_timeout": self.a.feedback_timeout,
                "commit_timeout": self.a.commit_timeout,
                "reveal_timeout": self.a.reveal_timeout,
                "max_turns": self.a.max_turns,
                "max_skips": self.a.max_skips,
            },
        })

        # 斷線的人不會再送承諾，把他們也當成「已定案」，否則整局會白等
        # commit_timeout 秒。
        def settled():
            return all(p.commit is not None or self.gone(p.pid) for p in players)

        await self.wait_until(settled, self.a.commit_timeout)

        for p in players:
            if p.commit is None:
                p.status = "REMOVED"    # 移出本局，之後連廣播都收不到

        order = [p.pid for p in players if p.status == "ALIVE"]
        if len(order) < MIN_PLAYERS:
            return None

        self.broadcast({
            "type": "COMMITS",
            "order": order,
            "commits": {pid: self.players[pid].commit for pid in order},
        })
        return order

    # ==================================================================
    # B12：回合循環（§12.3）
    # ==================================================================

    async def play_phase(self, order: list) -> str:
        """跑回合直到剩下一人或回合數用盡，回傳 `GAME_OVER.reason`。"""
        self.phase = "PLAY"
        pos = 0

        while True:
            alive = self.alive_ids(order)
            if len(alive) <= 1:
                return "LAST_PLAYER"
            if self.turn_id >= self.a.max_turns:
                return "MAX_TURNS"

            while self.players[order[pos % len(order)]].status != "ALIVE":
                pos += 1
            guesser = order[pos % len(order)]
            pos += 1

            self.turn_id += 1
            await self.run_turn(guesser, alive)

    async def run_turn(self, guesser: str, alive: list) -> None:
        """一個回合：`TURN` → 等 `GUESS` → `GUESS_MADE` → 等 `FEEDBACK`
        → 一致性檢查 → `TURN_RESULT` → 淘汰揭示。
        """
        guess = await self.collect_guess(guesser, alive)
        if guess is None:
            self.announce_skip(guesser)
            return

        responders = [pid for pid in alive if pid != guesser]
        await self.collect_feedback(guesser, guess, responders)
        eliminated, forfeited, disqualified = self.settle_turn(guess, responders)

        self.broadcast({
            "type": "TURN_RESULT",
            "turn_id": self.turn_id,
            "guesser": guesser,
            "guess": guess,
            "skipped": False,
            "results": [{"player": pid, "a": a, "b": b}
                        for pid, (a, b) in self.feedback.items()],
            "eliminated": eliminated,
            "forfeited": forfeited,
            "disqualified": disqualified,
        })

        if eliminated:
            await self.collect_reveals([self.players[pid] for pid in eliminated], "ELIMINATED")

    async def collect_guess(self, guesser: str, alive: list) -> str | None:
        """開喊數窗，等 `GUESS`。逾時或斷線回傳 None。"""
        self.guesser = guesser
        self.cur_guess = None
        self.guess_open = True
        self.broadcast({"type": "TURN", "turn_id": self.turn_id,
                        "guesser": guesser, "alive": alive})

        await self.wait_until(lambda: not self.guess_open or self.gone(guesser),
                              self.a.guess_timeout)
        self.guess_open = False
        return self.cur_guess

    def announce_skip(self, guesser: str) -> None:
        """沒喊數的回合。連續未喊達 `--max-skips` 即判棄權（§12.3）。

        `skipped` 與 `guess is None` 必須同真同假，而且 `results` 一定是空的
        ——§11.3 會檢查這兩件事。
        """
        player = self.players[guesser]
        player.skips += 1
        forfeited = []
        if player.skips >= self.a.max_skips or self.gone(guesser):
            self.depart(player, "FORFEIT")
            forfeited.append(guesser)

        self.broadcast({
            "type": "TURN_RESULT",
            "turn_id": self.turn_id,
            "guesser": guesser,
            "guess": None,
            "skipped": True,
            "results": [],
            "eliminated": [],
            "forfeited": forfeited,
            "disqualified": [],
        })

    async def collect_feedback(self, guesser: str, guess: str, responders: list) -> None:
        """開回報窗，等所有回報者送出 `FEEDBACK`。

        **收齊之前一則都不公開**（§3、§14）。先洩漏任何一筆，後面的人就能
        據此反推並編造自己的回報，三道防線的第一道就破了。所以這裡只負責
        等，公開一律留給 `TURN_RESULT`。
        """
        self.players[guesser].skips = 0
        self.responders = responders
        self.feedback = {}
        self.fb_open = True
        self.broadcast({"type": "GUESS_MADE", "turn_id": self.turn_id,
                        "guesser": guesser, "guess": guess, "responders": responders})

        await self.wait_until(
            lambda: all(pid in self.feedback or self.gone(pid) for pid in responders),
            self.a.feedback_timeout)
        self.fb_open = False

    # ==================================================================
    # B13：每回合的公開一致性檢查（第一道防線）
    # ==================================================================

    def settle_turn(self, guess: str, responders: list) -> tuple:
        """用本回合的回報刪減候選集合，決定誰離場。

        對每位回報者維護「與他全部歷史回報都相容」的候選集合，從 5040 個開始
        逐回合刪減。**集合變成空集，表示沒有任何秘密數字能同時滿足他講過的
        每一句話**——那是數學上的矛盾，不是運氣問題，所以當回合取消資格。

        回報 4A0B 表示被猜中；沒回報的則依 §12.3 判棄權。
        """
        eliminated, forfeited, disqualified = [], [], []

        for pid in responders:
            player = self.players[pid]
            if pid not in self.feedback:
                self.depart(player, "FORFEIT")
                forfeited.append(pid)
                continue

            a, b = self.feedback[pid]
            player.records.append((self.turn_id, guess, a, b))
            player.cands = filter_candidates(player.cands, guess, a, b)

            if not player.cands:
                self.depart(player, "DQ")
                disqualified.append(pid)
            elif (a, b) == (4, 0):
                self.depart(player, "GUESSED")
                eliminated.append(pid)

        return eliminated, forfeited, disqualified

    # ==================================================================
    # B14／B15：揭示（第二、三道防線）
    # ==================================================================

    async def collect_reveals(self, players: list, reason: str) -> None:
        """向指定玩家要揭示，驗證後廣播 `REVEALED`。

        `REVEAL_REQUEST` **只送給當事人**，不廣播。驗證結果才公開。

        驗證不通過一律取消資格：沒揭示是 `NO_REVEAL`、秘密與承諾值對不上是
        `BAD_COMMIT`、歷史回報重算不符是 `INCONSISTENT`（並附上出錯的回合）。
        """
        self.reveal_wait = {p.pid for p in players}
        for p in players:
            if p.conn is not None and not p.conn.closed:
                p.conn.send({"type": "REVEAL_REQUEST", "reason": reason})

        await self.wait_until(lambda: not self.reveal_wait, self.a.reveal_timeout)
        self.reveal_wait = set()

        for p in players:
            secret, salt = p.reveal if p.reveal is not None else (None, None)
            verdict, bad_turns = verify_reveal(p.commit, p.records, secret, salt)
            if verdict != "OK":
                self.depart(p, "DQ")
            self.broadcast({
                "type": "REVEALED",
                "player": p.pid,
                "secret": secret,
                "salt": salt,
                "verdict": verdict,
                "bad_turns": bad_turns,
            })

    async def end_phase(self) -> None:
        """終局揭示：仍存活者也要驗，通過才算 `SURVIVOR`（§14）。"""
        self.phase = "END"
        survivors = [p for p in self.players.values() if p.status == "ALIVE"]
        if survivors:
            await self.collect_reveals(survivors, "GAME_END")
        for p in survivors:
            if p.status == "ALIVE":
                p.status = "SURVIVOR"

    # ==================================================================
    # 終局訊息
    # ==================================================================

    def broadcast_game_over(self, reason: str) -> None:
        """名次由 `compute_ranking()` 算（§14.3 的標準競賽排名）。"""
        rows = compute_ranking([(p.pid, p.status, p.out_turn) for p in self.joined_players()])
        self.broadcast({
            "type": "GAME_OVER",
            "game_id": self.game_id,
            "reason": reason,
            "turns": self.turn_id,
            "ranking": [{"player": pid, "status": status, "out_turn": out_turn, "rank": rank}
                        for pid, status, out_turn, rank in rows],
        })

    def broadcast_aborted(self) -> None:
        """人數不足以開局或繼續。`turns` 與 `ranking` 必須是 0 與空陣列（§11.3）。"""
        self.broadcast({"type": "GAME_OVER", "game_id": self.game_id,
                        "reason": "ABORTED", "turns": 0, "ranking": []})

    # ==================================================================
    # B10–B15：主流程
    # ==================================================================

    async def run_game(self) -> None:
        """主持一局：LOBBY → COMMIT → PLAY → END → OVER。

        `finally` 裡的三件事不論中途怎麼結束都要做：標記 `OVER`（協定層看到
        就不再收任何訊息）、關閉本局所有連線（§5.3-6，含從未送 `HELLO` 的）、
        關掉日誌檔。
        """
        self.game_id = self.new_game_id()
        self.open_log(self.game_id)
        try:
            if not await self.lobby_phase():
                self.broadcast_aborted()
                return
            order = await self.commit_phase()
            if order is None:
                self.broadcast_aborted()
                return
            reason = await self.play_phase(order)
            await self.end_phase()
            self.broadcast_game_over(reason)
        finally:
            self.phase = "OVER"
            self.games_played += 1
            self.close_all_conns()
            self.close_log()

    # ==================================================================
    # B18：行程生命週期（§5.3-6、§5.3-7、§5.3-8）
    # ==================================================================

    async def serve(self) -> None:
        """開埠、連續主持 `--games` 局，然後讓行程自行結束。

        規範點名的三件事都在這裡：

        * **`SO_REUSEADDR`**（§5.3-7）。自我測試會連續開關同一個埠，不設這個
          選項會卡在前一次的 TIME_WAIT 而 bind 失敗。
        * **`--games` 迴圈**（§5.3-6）。`0` 表示不限；做完指定局數就回傳，
          監聽埠與主控台執行緒都不留，行程自行結束。
        * **主控台只在真的終端機才讀**（§5.3-8）。stdin 是 `/dev/null`、`NUL`、
          管線或已關閉時完全不讀，伺服器照樣正常運作。

        局與局之間監聽埠一直開著，所以上一局結束後連進來的人會留在大廳等
        下一局。`run_game()` 自己會在 `finally` 裡關掉本局所有連線並遞增
        `games_played`，這裡只負責「開場前清乾淨、散場後收乾淨」。
        """
        if self.a.bots:
            self._say("warning: --bots 尚未實作（B17），不會啟動 AI 子行程",
                      stream=sys.stderr)

        loop = asyncio.get_running_loop()
        # reuse_address 就是 SO_REUSEADDR；asyncio 在 POSIX 預設開啟、
        # Windows 預設關閉，所以明確指定，三種作業系統行為才一致。
        server = await asyncio.start_server(
            self.handle_conn, self.a.host, self.a.port, reuse_address=True)

        bound = ", ".join(self._socket_names(server))
        games = "不限" if self.a.games == 0 else str(self.a.games)
        self._say(f"BC/1.0 server 監聽 {bound}；--players {self.a.players}、--games {games}")
        if self._stdin_is_console():
            self._say("主控台：輸入 start 可在 2 人以上時提早開局")
            threading.Thread(target=self._console_loop, args=(loop,),
                             daemon=True, name="bc-console").start()

        try:
            while self.a.games == 0 or self.games_played < self.a.games:
                self.reset_for_new_game()
                await self.run_game()
                self._say(f"第 {self.games_played} 局結束（game_id={self.game_id}）")
        finally:
            server.close()
            await server.wait_closed()
            self.close_all_conns()
            self.close_log()

    # ------------------------------------------------------------------
    # 主控台（§5.3-8）
    # ------------------------------------------------------------------

    @staticmethod
    def _stdin_is_console() -> bool:
        """stdin 是不是真的終端機。

        POSIX 上 `isatty()` 就夠了。Windows 不行：`NUL` 也是字元裝置，
        `isatty()` 對它一樣回 True，所以 `stdin=DEVNULL` 啟動的伺服器會被
        誤判成有主控台。只有真的 console 才拿得到 console mode，用這個補判。
        """
        stdin = sys.stdin
        if stdin is None:
            return False
        try:
            if not stdin.isatty():
                return False
            fd = stdin.fileno()
        except (OSError, ValueError):
            return False
        if os.name != "nt":
            return True
        try:
            import ctypes
            import msvcrt

            mode = ctypes.c_ulong()
            handle = msvcrt.get_osfhandle(fd)
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except (OSError, ValueError, AttributeError, ImportError):
            return False

    def _console_loop(self, loop) -> None:
        """在獨立的 daemon 執行緒裡讀主控台命令（§5.3-8）。

        不用 `loop.run_in_executor()`：`asyncio.run()` 收尾時會 join 預設執行緒
        池，而 `readline()` 會一直卡著等按鍵，行程就永遠不會結束——正好違反
        §5.3-6 的「主持完指定局數後行程必須自行結束」。daemon 執行緒不會被
        join，所以該結束的時候結束得掉。

        迴圈在 EOF 自然結束（`for line in sys.stdin`）。
        """
        try:
            for line in sys.stdin:
                command = line.strip().lower()
                if command == "start":
                    loop.call_soon_threadsafe(self._request_start)
                elif command:
                    self._say(f"未知的主控台命令：{command!r}（只支援 start）",
                              stream=sys.stderr)
        except (OSError, ValueError):
            # stdin 中途被關掉：不讀就是了，不能影響對局。
            pass

    def _request_start(self) -> None:
        """主控台的 `start`：在事件迴圈裡改欄位，然後敲鐘。

        `lobby_phase()` 的 `ready()` 讀 `manual_start`，所以這裡一定要
        `changed.set()`，否則大廳會繼續睡到有人加入才醒。
        """
        self.manual_start = True
        self.changed.set()

    # ------------------------------------------------------------------
    # 局與局之間
    # ------------------------------------------------------------------

    def reset_for_new_game(self) -> None:
        """清掉上一局的狀態，讓大廳重新開放（§5.3-6）。

        只在 `run_game()` 之前呼叫，中間沒有 await，所以不會清掉兩局之間
        剛連進來的人：那時還沒有人能完成 `HELLO`。連線本身不清空，只丟掉
        已關閉的——監聽埠一直開著，新連線要留給下一局。
        """
        self.phase = "LOBBY"
        self.turn_id = 0
        self.guesser = None
        self.guess_open = False
        self.cur_guess = None
        self.fb_open = False
        self.responders = []
        self.feedback = {}
        self.reveal_wait = set()
        self.players = {}
        self.next_id = 1
        self.manual_start = False
        self.game_id = None
        self.conns = {conn for conn in self.conns if not conn.closed}
        self.changed.set()

    # ------------------------------------------------------------------
    # 給莊家看的輸出
    # ------------------------------------------------------------------

    @staticmethod
    def _say(text: str, *, stream=None) -> None:
        """印一行給莊家看的訊息，絕不讓它弄掉伺服器。

        Windows 把 stdout 導向檔案或管線時用的是系統 ANSI 編碼（這台機器是
        cp932），中文可能編不出來而拋 `UnicodeEncodeError`。伺服器不能因為
        一行提示字就當掉（§5.3-1），所以編不出來就退回 ASCII 轉義。
        """
        if stream is None:
            stream = sys.stdout
        try:
            print(text, file=stream, flush=True)
        except UnicodeEncodeError:
            print(text.encode("ascii", "backslashreplace").decode("ascii"),
                  file=stream, flush=True)
        except (OSError, ValueError):
            pass            # stdout 被關掉也不該影響對局

    @staticmethod
    def _socket_names(server) -> list:
        """監聽中的位址，純粹為了印出來給莊家看。"""
        names = []
        for sock in server.sockets or ():
            try:
                host, port = sock.getsockname()[:2]
            except OSError:          # pragma: no cover - 極少見
                continue
            names.append(f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}")
        return names or ["?"]
