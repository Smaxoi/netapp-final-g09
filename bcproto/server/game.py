"""B8/B9/B16：遊戲引擎的基礎設施。**這個檔案由彭明山一個人改。**

目前完成的是不依賴協定層的三塊：

* **B8** `broadcast()`、`broadcast_lobby()`、`audience()`——廣播對象的判定規則
  （§11.3）只寫在這裡一次，協定層的 `on_chat` 直接呼叫 `broadcast()` 就好，
  不必自己判斷誰該收。
* **B9** `wait_until()`——把「協定層改欄位 + 敲鐘」轉成遊戲迴圈的 `await`。
* **B16** `log()` / `log_raw()` / `open_log()` / `close_log()`——§5.6 的
  JSON Lines 日誌，一局一檔，每筆立即 flush。

`run_game()` 的階段流程（B10–B15）要等協定層的 A10／A11 能讓玩家真的加入
才能接上，先留介面。
"""

import asyncio
import json
import os
import time
import uuid

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
    # B10–B15：階段流程。等 A10／A11 能讓玩家加入後接上。
    # ==================================================================

    async def run_game(self) -> None:
        """主持一局：LOBBY → COMMIT → PLAY → END → OVER。"""
        raise NotImplementedError("B10–B15 尚未實作（彭明山；需 A10／A11）")
