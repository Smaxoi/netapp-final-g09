"""A8～A13：連線傳送、接收、分派、訊息處理器與協定防護。

send/error/close 都是同步方法，broadcast 不須等待慢速客戶端。
正常 close 會依序送完已排入的訊息；fatal ERROR 排入後也走這條路徑。
佇列滿、寫入逾時或失敗時，只中止這條連線。
所有狀態沿用 Server 狀態契約；遊戲排程、廣播與日誌由 GameMixin 提供。
"""

import asyncio
from collections.abc import Callable

from ..errors import BAD_JSON, LINE_TOO_LONG, TIMEOUT, TOO_MANY_ERRORS, error_message
from ..validators import is_int, valid_timeout
from ..wire import MAX_LINE, decode_line, encode
from .checks import PlayerState, check_message


class Conn:
    """須在 running event loop 中建立；由同一個 loop 呼叫同步方法。

    writer 為 asyncio.StreamWriter。on_close 可用來 changed.set() 喚醒
    遊戲引擎，在連線邏輯上關閉時同步呼叫一次，不等待作業系統釋放連線。
    failure 留存首個 I/O、逾時或佇列溢位例外，供後續日誌使用。
    """

    def __init__(self, writer: asyncio.StreamWriter, *, max_pending: int = 128,
                 write_timeout: float = 5.0, close_timeout: float = 1.0,
                 on_close: Callable[["Conn"], None] | None = None, srv=None):
        if not is_int(max_pending) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        if not valid_timeout(write_timeout) or not valid_timeout(close_timeout):
            raise ValueError("write_timeout and close_timeout must be positive finite seconds")
        self.writer = writer
        self.srv = srv
        self.player: PlayerState | None = None
        self.error_count = 0
        self.failure: Exception | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_pending)
        self._write_timeout = write_timeout
        self._close_timeout = close_timeout
        self._on_close = on_close
        self._closing = False
        self._closed = asyncio.Event()
        self._started = False
        self.writer_task = asyncio.create_task(self._write_loop(), name="bc-conn-writer")

    @property
    def connected(self) -> bool:
        """供 B8/B9 判斷是否仍可傳送；開始 close 後立即為 False。"""
        return not self._closing and not self.writer.is_closing()

    @property
    def closed(self) -> bool:
        """邏輯上已斷線，供 GameMixin.audience/wait_until 立即判定。"""
        return self._closing

    def label(self) -> str:
        """日誌 peer：完成 HELLO 後為 player_id，之前為 IP:埠號。"""
        if self.player is not None:
            return self.player.pid
        get_info = getattr(self.writer, "get_extra_info", None)
        peer = get_info("peername") if get_info is not None else None
        if peer is None:
            return "unknown"
        host, port = peer[:2]
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    def send(self, msg: dict) -> None:
        """立即編碼並入列，保留當下內容；已關閉連線的 send 不做事。

        編碼錯誤代表本地訊息錯誤，直接向呼叫端拋出；佇列滿則只中止
        本連線，不拋 QueueFull，讓 broadcast 能繼續傳給其他玩家。
        """
        if self._closing:
            return
        data = encode(msg)
        if self.srv is not None:
            try:
                self.srv.log("send", self.label(), msg)
            except Exception as error:
                self._record_failure(error)
                self.abort()
                return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            self._record_failure(BufferError("Connection send queue is full"))
            self.abort()

    def error(self, code: str, message: str | None = None,
              fatal: bool | None = None, *, message_type: str | None = None) -> None:
        """建構並排入 ERROR，致命時送完再關閉。

        支援契約的 error(code, message, fatal) 呼叫；fatal 省略時用 A6
        與 self.player 判定。首次 HELLO 的欄位錯誤需傳 message_type="HELLO"。
        A13 的計數屬於連線，不因 HELLO 或合法訊息而歸零。第 21 次先
        排入本次 ERROR，再排入致命 TOO_MANY_ERRORS，最後才關閉。
        """
        if self._closing:
            return
        from . import MAX_ERRORS

        msg = error_message(code, message, message_type=message_type,
                            hello_done=self.player is not None)
        if fatal is not None:
            if type(fatal) is not bool:
                raise TypeError("fatal must be a boolean")
            msg["fatal"] = fatal
        self.error_count += 1
        self.send(msg)
        if self.error_count > MAX_ERRORS and code != TOO_MANY_ERRORS:
            # 不遞迴呼叫 error()，避免把自動追加的錯誤再次計入。
            # 即使第 21 次本身是致命錯誤，也要先排完這兩則才關閉。
            self.send(error_message(TOO_MANY_ERRORS))
            self.close()
        elif msg["fatal"]:
            self.close()

    def close(self) -> None:
        """停止接受新訊息，依 FIFO 送完已排入資料後關閉；可重複呼叫。"""
        if self._closing:
            return
        self._begin_close()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # 佇列滿時 writer 仍會送完資料，在下一次迴圈發現 closing 後結束。
            pass

    def abort(self) -> None:
        """立即放棄待送資料並中止這條連線，用於過慢／故障客戶端。"""
        if self._closed.is_set():
            return
        self._begin_close()
        self._discard_pending()
        self._abort_transport()
        # 尚未開始的 task 若直接 cancel，不會進入 finally，無法完成清理。
        if self._started and not self.writer_task.done():
            self.writer_task.cancel()

    async def wait_closed(self) -> None:
        """等待 writer 與關閉清理完成；取消此等待不會取消 writer task。"""
        await self._closed.wait()

    def _record_failure(self, error: Exception) -> None:
        if self.failure is None:
            self.failure = error

    def _begin_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception as error:
                self._record_failure(error)

    def _abort_transport(self) -> None:
        try:
            self.writer.transport.abort()
        except Exception as error:
            self._record_failure(error)

    def _discard_pending(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def _write_loop(self) -> None:
        self._started = True
        try:
            while not (self._closing and self._queue.empty()):
                data = await self._queue.get()
                try:
                    if data is None:
                        break
                    self.writer.write(data)
                    await asyncio.wait_for(self.writer.drain(), self._write_timeout)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            self._abort_transport()
        except Exception as error:
            self._record_failure(error)
            self._abort_transport()
        finally:
            self._begin_close()
            self._discard_pending()
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), self._close_timeout)
            except Exception as error:
                self._record_failure(error)
                self._abort_transport()
            finally:
                self._closed.set()


class NetMixin:
    """A9～A13 的連線管理、訊息分派與各類訊息處理。"""

    # ------------------------------------------------------------------
    # A8：Conn 類別其實是獨立的類別而不是 mixin 方法，
    #     寫在這個檔案裡就好（class Conn: ...），B 只透過
    #     conn.send(dict) / conn.error(code, msg, fatal) / conn.close()
    #     / conn.label() 這四個介面使用它。
    # ------------------------------------------------------------------

    async def handle_conn(self, reader, writer):
        """A9：accept 一條連線到結束。

        - 加入 `self.conns`，結束時移除
        - 10 秒內未完成 `HELLO` → 致命 `TIMEOUT` 並斷線
        - 依 LF 切行、保留未完成部分；含 LF 超過 4096 bytes 即拒絕
        - 每行交給 `decode_line()`，再交給 `dispatch()`
        """
        from . import HELLO_TIMEOUT, MAX_OUTQUEUE

        loop = asyncio.get_running_loop()
        deadline = loop.time() + HELLO_TIMEOUT
        disconnected = asyncio.Event()

        def on_close(connection):
            disconnected.set()
            self._on_disconnect(connection)

        conn = Conn(writer, srv=self, max_pending=MAX_OUTQUEUE, on_close=on_close)
        self.conns.add(conn)
        self.changed.set()
        closed_wait = asyncio.create_task(disconnected.wait())
        pending = bytearray()
        try:
            while conn.connected:
                remaining = None if conn.player is not None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    conn.error(TIMEOUT)
                    break
                # 最多保留 4097 bytes：不依賴 StreamReader 預設 64 KiB 的
                # readline limit，沒有 LF 的超長輸入也能及時拒絕。
                chunk = await self._read_chunk(
                    reader, MAX_LINE + 1 - len(pending), closed_wait, remaining)
                if chunk is None:
                    break
                if not chunk:
                    if pending and self.phase != "OVER":
                        self.log_raw("recv", conn.label(), pending.decode("utf-8", errors="replace"))
                        conn.error(BAD_JSON)  # EOF 前未以 LF 結尾，不接受半則訊息。
                    break
                pending.extend(chunk)
                while conn.connected:
                    if self.phase == "OVER":
                        break
                    end = pending.find(b"\n")
                    if end < 0:
                        if len(pending) > MAX_LINE:
                            self._line_too_long(conn, pending)
                        break
                    if end + 1 > MAX_LINE:
                        self._line_too_long(conn, pending)
                        break
                    raw = bytes(pending[:end + 1])
                    del pending[:end + 1]
                    if conn.player is None and loop.time() >= deadline:
                        conn.error(TIMEOUT)
                        break
                    msg, error = decode_line(raw)
                    if error:
                        self.log_raw("recv", conn.label(), raw.decode("utf-8", errors="replace"))
                        conn.error(error)
                    elif msg is not None:
                        self.log("recv", conn.label(), msg)
                        self.dispatch(conn, msg)
                if self.phase == "OVER":
                    break
        except TimeoutError:
            if conn.connected and self.phase != "OVER":
                conn.error(TIMEOUT)
        except asyncio.CancelledError:
            conn.abort()
            raise
        except Exception as error:
            conn._record_failure(error)
            conn.abort()
        finally:
            conn.close()
            closed_wait.cancel()
            await asyncio.gather(closed_wait, return_exceptions=True)
            try:
                await conn.wait_closed()
            finally:
                self.conns.discard(conn)
                self.changed.set()

    @staticmethod
    async def _read_chunk(reader, size, closed_wait, timeout):
        """讀取與本連線關閉競速；取消時不留下背景 read task。"""
        read_task = asyncio.create_task(reader.read(size))
        try:
            done, _ = await asyncio.wait(
                (read_task, closed_wait), timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError
            if closed_wait in done:
                return None
            return read_task.result()
        finally:
            if not read_task.done():
                read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)

    def _line_too_long(self, conn, raw):
        self.log_raw("recv", conn.label(), raw.decode("utf-8", errors="replace"))
        conn.error(LINE_TOO_LONG)

    def _on_disconnect(self, conn):
        """大廳斷線即移除；對局中的棄權與 NO_REVEAL 留給遊戲判定點。"""
        player = conn.player
        if (self.phase == "LOBBY" and player is not None
                and self.players.get(player.pid) is player):
            del self.players[player.pid]
            # send() 佇列滿時可能在另一個 broadcast_lobby() 裡同步回呼。
            # 延到下一輪廣播，避免先送新名單、外層再送舊名單而倒序。
            asyncio.get_running_loop().call_soon(self._broadcast_lobby_if_open)
        self.changed.set()

    def _broadcast_lobby_if_open(self):
        if self.phase == "LOBBY":
            self.broadcast_lobby()

    def _validate(self, conn, msg):
        if self.phase == "OVER" or not conn.connected:
            return False
        code = check_message(msg, self, conn.player)
        if code is not None:
            kind = msg.get("type") if isinstance(msg, dict) else None
            conn.error(code, message_type=kind)
            return False
        return True

    def dispatch(self, conn, msg):
        """A10：依 `msg["type"]` 分派到下面的 handler。

        - 不認識的類型 → 非致命 `UNKNOWN_TYPE`
        - 尚未完成 `HELLO` 就送非 `HELLO` 的訊息 → **致命** `UNEXPECTED`
        - 已完成 `HELLO` 的連線再送 `HELLO` → **非致命** `UNEXPECTED`，連線保留
        """
        if not self._validate(conn, msg):
            return
        handlers = {
            "HELLO": self.on_hello, "COMMIT": self.on_commit,
            "GUESS": self.on_guess, "FEEDBACK": self.on_feedback,
            "REVEAL": self.on_reveal, "CHAT": self.on_chat,
            "PING": self.on_ping, "PONG": self.on_pong, "BYE": self.on_bye,
        }
        handlers[msg["type"]](conn, msg)

    def on_hello(self, conn, m):
        """檢查順序（§13）：

        1. `protocol`/`name`/`team`/`kind` 四個字串欄位齊全 → `BAD_FIELD`（致命）
        2. `protocol` 符合 ``BC/1.[0-9]+``        → `VERSION_UNSUPPORTED`（致命）
        3. `name`/`team` 1–20 字無控制字元、
           `kind` ∈ {human, ai}                  → `INVALID_VALUE`（致命）
        4. 讀 `phase`，必須在大廳階段             → `GAME_IN_PROGRESS`（致命）
        5. 讀 `len(players)`，人數未滿            → `ROOM_FULL`（致命）
        6. 讀 `players`，名稱未重複               → `NAME_TAKEN`（致命）

        通過後：`pid = f"p{self.next_id}"`、`next_id += 1`、建 `Player`、
        送 `WELCOME`、呼叫 `self.broadcast_lobby()`、`changed.set()`。
        """
        from . import Player

        try:
            if not self._validate(conn, m):
                return
            pid = f"p{self.next_id}"
            player = Player(pid, m["name"], m["team"], m["kind"], conn)
            self.next_id += 1
            self.players[pid] = player
            conn.player = player
            conn.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": pid})
            self.broadcast_lobby()
        finally:
            self.changed.set()

    def on_commit(self, conn, m):
        """1. `commit` 是字串                → `BAD_FIELD`
        2. `phase == "COMMIT"` 且該玩家 ALIVE  → `UNEXPECTED`
        3. `p.commit is None`                  → `DUPLICATE`
        4. 64 個小寫 hex                       → `INVALID_VALUE`

        通過後：`p.commit = c`、`changed.set()`。
        """
        try:
            if self._validate(conn, m):
                conn.player.commit = m["commit"]
        finally:
            self.changed.set()

    def on_guess(self, conn, m):
        """1. `turn_id` 是整數（**排除 bool**）、`guess` 是字串 → `BAD_FIELD`
        2. 讀 `phase`，須為 `"PLAY"`                → `UNEXPECTED`
        3. 讀 `turn_id`，須等於目前回合              → `STALE_TURN`
        4. 讀 `guesser`，送出者須是喊數者            → `NOT_YOUR_TURN`
        5. 讀 `guess_open`，窗還開著                 → `UNEXPECTED`
        6. 四個互異數字                              → `INVALID_VALUE`

        通過後：`cur_guess = g`、`guess_open = False`、`changed.set()`。
        """
        try:
            if self._validate(conn, m):
                self.cur_guess = m["guess"]
                self.guess_open = False
        finally:
            self.changed.set()

    def on_feedback(self, conn, m):
        """1. `turn_id`/`a`/`b` 都是整數（**排除 bool**） → `BAD_FIELD`
        2. 讀 `phase`，須為 `"PLAY"`                     → `UNEXPECTED`
        3. 讀 `turn_id`，須等於目前回合                   → `STALE_TURN`
        4. 讀 `fb_open`、`responders`，窗開著且自己在名單內 → `UNEXPECTED`
        5. 讀 `feedback`，尚未回報過                      → `DUPLICATE`
        6. `(a, b)` 在 14 種合法組合內（**3A1B 不合法**）  → `INVALID_VALUE`

        通過後：`feedback[pid] = (a, b)`（**只填單一項目，不可整個重新賦值**）、
        `changed.set()`。
        """
        try:
            if self._validate(conn, m):
                self.feedback[conn.player.pid] = (m["a"], m["b"])
        finally:
            self.changed.set()

    def on_reveal(self, conn, m):
        """1. `secret`/`salt` 都是字串                     → `BAD_FIELD`
        2. 讀 `reveal_wait`、`p.reveal`：已揭示過 → `DUPLICATE`；
           不在等待名單中 → `UNEXPECTED`
        3. `secret` 四位互異、`salt` 32 個小寫 hex        → `INVALID_VALUE`

        通過後：`p.reveal = (s, salt)`、`reveal_wait.discard(pid)`、`changed.set()`。

        注意：格式正確但**內容不實**的揭示（秘密與承諾值不符）**不是協定錯誤**，
        由遊戲引擎在驗證階段判成 `BAD_COMMIT`。這裡只檢查格式。
        """
        try:
            if self._validate(conn, m):
                conn.player.reveal = (m["secret"], m["salt"])
                self.reveal_wait.discard(conn.player.pid)
        finally:
            self.changed.set()

    def on_chat(self, conn, m):
        """`BAD_FIELD`（`text` 不是字串）→ `INVALID_VALUE`（不是 1–200 字或含控制字元）
        → 通過則 `self.broadcast({"type": "CHAT", "from": pid, "text": text})`。
        """
        try:
            if self._validate(conn, m):
                self.broadcast({"type": "CHAT", "from": conn.player.pid, "text": m["text"]})
        finally:
            self.changed.set()

    def on_ping(self, conn, m):
        """回 `PONG`。`PING` 帶 `nonce` 時**必須原樣回傳**。"""
        try:
            if self._validate(conn, m):
                reply = {"type": "PONG"}
                if "nonce" in m:
                    reply["nonce"] = m["nonce"]
                conn.send(reply)
        finally:
            self.changed.set()

    def on_pong(self, conn, m):
        """驗證選填 nonce 的型別後忽略，不對 PONG 再回覆訊息。"""
        try:
            self._validate(conn, m)
        finally:
            self.changed.set()

    def on_bye(self, conn, m):
        """`conn.close()`，效果等同斷線。"""
        try:
            if self._validate(conn, m):
                conn.close()
        finally:
            self.changed.set()
