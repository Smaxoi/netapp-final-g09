"""A8–A13：協定層。**這個檔案由唐偉捷一個人改。**

彭明山先推這個空殼，讓 handler 可以立刻開工，不必等遊戲流程寫完。
每個方法的 docstring 就是《Server 狀態契約》§9 的速查表：檢查順序是規範 §13
規定的，**不可調換**——同一則訊息犯多條時只回第一個符合的錯誤碼。

共用的檢查工具都已經寫好了，直接用：

    from ..wire import decode_line, encode
    from ..messages import check_client_fields, check_client_values, CLIENT_TYPES
    from ..errors import error_message, is_fatal

三條鐵律：

1. **每個 `on_*` 的最後一行無條件 `self.changed.set()`**，不要判斷這次有沒有
   真的改到狀態。漏一次，遊戲迴圈會睡到逾時，整局卡住且不會報錯。
2. 收到不合協定的訊息時**只回一則 `ERROR`** 給該連線，忽略該訊息，
   **遊戲狀態不變**；`fatal` 為真則送出後立即關閉連線。
3. 每一則收送的訊息都要寫日誌：`self.log("recv", conn.label(), msg)` 與
   `self.log("send", conn.label(), msg)`（§5.6，B16 已實作 `log()`）。
   無法解析的輸入用 `self.log_raw("recv", conn.label(), text)`。
"""


class NetMixin:
    """連線管理與訊息分派。方法簽名已定，實作留給唐偉捷。"""

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
        - `readline()` 切行；單行超過 4096 bytes → 致命 `LINE_TOO_LONG`
        - 每行交給 `decode_line()`，再交給 `dispatch()`
        """
        raise NotImplementedError("A9 handle_conn 尚未實作（唐偉捷）")

    def dispatch(self, conn, msg):
        """A10：依 `msg["type"]` 分派到下面的 handler。

        - 不認識的類型 → 非致命 `UNKNOWN_TYPE`
        - 尚未完成 `HELLO` 就送非 `HELLO` 的訊息 → **致命** `UNEXPECTED`
        - 已完成 `HELLO` 的連線再送 `HELLO` → **非致命** `UNEXPECTED`，連線保留
        """
        raise NotImplementedError("A10 dispatch 尚未實作（唐偉捷）")

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
        raise NotImplementedError("A10 on_hello 尚未實作（唐偉捷）")

    def on_commit(self, conn, m):
        """1. `commit` 是字串                → `BAD_FIELD`
        2. `phase == "COMMIT"` 且該玩家 ALIVE  → `UNEXPECTED`
        3. `p.commit is None`                  → `DUPLICATE`
        4. 64 個小寫 hex                       → `INVALID_VALUE`

        通過後：`p.commit = c`、`changed.set()`。
        """
        raise NotImplementedError("A11 on_commit 尚未實作（唐偉捷）")

    def on_guess(self, conn, m):
        """1. `turn_id` 是整數（**排除 bool**）、`guess` 是字串 → `BAD_FIELD`
        2. 讀 `phase`，須為 `"PLAY"`                → `UNEXPECTED`
        3. 讀 `turn_id`，須等於目前回合              → `STALE_TURN`
        4. 讀 `guesser`，送出者須是喊數者            → `NOT_YOUR_TURN`
        5. 讀 `guess_open`，窗還開著                 → `UNEXPECTED`
        6. 四個互異數字                              → `INVALID_VALUE`

        通過後：`cur_guess = g`、`guess_open = False`、`changed.set()`。
        """
        raise NotImplementedError("A11 on_guess 尚未實作（唐偉捷）")

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
        raise NotImplementedError("A11 on_feedback 尚未實作（唐偉捷）")

    def on_reveal(self, conn, m):
        """1. `secret`/`salt` 都是字串                     → `BAD_FIELD`
        2. 讀 `reveal_wait`、`p.reveal`：已揭示過 → `DUPLICATE`；
           不在等待名單中 → `UNEXPECTED`
        3. `secret` 四位互異、`salt` 32 個小寫 hex        → `INVALID_VALUE`

        通過後：`p.reveal = (s, salt)`、`reveal_wait.discard(pid)`、`changed.set()`。

        注意：格式正確但**內容不實**的揭示（秘密與承諾值不符）**不是協定錯誤**，
        由遊戲引擎在驗證階段判成 `BAD_COMMIT`。這裡只檢查格式。
        """
        raise NotImplementedError("A11 on_reveal 尚未實作（唐偉捷）")

    def on_chat(self, conn, m):
        """`BAD_FIELD`（`text` 不是字串）→ `INVALID_VALUE`（不是 1–200 字或含控制字元）
        → 通過則 `self.broadcast({"type": "CHAT", "from": pid, "text": text})`。
        """
        raise NotImplementedError("A12 on_chat 尚未實作（唐偉捷）")

    def on_ping(self, conn, m):
        """回 `PONG`。`PING` 帶 `nonce` 時**必須原樣回傳**。"""
        raise NotImplementedError("A12 on_ping 尚未實作（唐偉捷）")

    def on_pong(self, conn, m):
        """忽略。"""
        raise NotImplementedError("A12 on_pong 尚未實作（唐偉捷）")

    def on_bye(self, conn, m):
        """`conn.close()`，效果等同斷線。"""
        raise NotImplementedError("A12 on_bye 尚未實作（唐偉捷）")
