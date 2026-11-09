# Server 狀態契約

> **給唐偉捷（A 協定層）與彭明山（B 遊戲引擎）**
>
> 這兩塊會同時操作同一個 `Server` 物件。欄位名稱與讀寫權責沒先講好，整合那天一定對不上。
> 本文就是那份約定。

---

## 1. 問題在哪

兩人的程式跑在**不同的 asyncio task** 裡：

```
唐偉捷的 handle_conn（每條連線一個 task）       彭明山的 run_game（一個 task）
    │                                               │
    │  讀 socket → 解析 → 驗證 → 改狀態             │  開窗 → await 等 → 讀結果 → 廣播
    │                    ↓                          │          ↑
    └─────────────►  同一個 Server 物件  ◄──────────┘
```

**唐偉捷不能直接呼叫彭明山的流程**，因為後者正卡在 `await` 上。
所以唯一的溝通方式是：**A 改欄位 → 發訊號 → B 醒來讀欄位。**

---

## 2. 運作模型：開窗 / 填答 / 讀取

**彭明山負責「現在開放收什麼」，唐偉捷負責「檢查合不合規、填進去」。**

以第 3 回合為例，完整走一遍：

```
① 彭明山「開窗」
     self.turn_id    = 3
     self.guesser    = "p2"
     self.cur_guess  = None
     self.guess_open = True
     self.broadcast({"type": "TURN", ...})
     await self.wait_until(lambda: self.cur_guess is not None, guess_timeout)
                                               ↑ 睡在這裡

② p2 送 GUESS 進來，唐偉捷的 on_guess 接到
     phase      == "PLAY"      ? 否 → ERROR UNEXPECTED
     turn_id    == 3           ? 否 → ERROR STALE_TURN
     pid        == guesser     ? 否 → ERROR NOT_YOUR_TURN
     guess_open == True        ? 否 → ERROR UNEXPECTED
     四位相異數字              ? 否 → ERROR INVALID_VALUE
     ──────── 全過 ────────
     self.cur_guess  = "0524"        ← 填答
     self.guess_open = False
     self.changed.set()              ← 敲鐘

③ 彭明山醒來
     guess = self.cur_guess          ← 讀取
     self.broadcast({"type": "GUESS_MADE", ...})
     再開下一個窗（fb_open = True、responders = [...]）
```

---

## 3. Server 的欄位

| 欄位 | 型別 | 寫 | 讀 | 意義 |
|---|---|---|---|---|
| `phase` | `str` | **B** | A | `LOBBY` / `COMMIT` / `PLAY` / `END` / `OVER` |
| `turn_id` | `int` | **B** | A | 目前回合，自 1 起 |
| `guesser` | `str \| None` | **B** | A | 本回合喊數者 `pid` |
| `guess_open` | `bool` | **B** 開 / **A** 關 | 兩邊 | ⚠️ 雙寫，見 §5 |
| `cur_guess` | `str \| None` | **A** | B | 收到的喊數 |
| `fb_open` | `bool` | **B** | A | 是否還收 `FEEDBACK` |
| `responders` | `list[str]` | **B** | A | 本回合該回報的 `pid`（= `alive` 去掉喊數者） |
| `feedback` | `dict[str, tuple[int,int]]` | **A** 填 / **B** 清空 | 兩邊 | ⚠️ 雙寫 |
| `reveal_wait` | `set[str]` | **B** 填 / **A** 移除 | 兩邊 | ⚠️ 雙寫 |
| `players` | `dict[str, Player]` | **A** 新增 / **B** 改狀態 | 兩邊 | 本局玩家 |
| `next_id` | `int` | **A** | — | 下一個 `player_id` 編號，每局自 1 起 |
| `changed` | `asyncio.Event` | **A** `.set()` | B `await` | 狀態變動訊號 |
| `game_id` | `str` | **B** | A | 本局識別碼 |
| `manual_start` | `bool` | B（console） | B | 莊家輸入 `start` |
| `conns` | `set` | **A** 增刪 | B | 所有連線，**含尚未完成 `HELLO` 者**；局末 B 要全部關掉（§5.3-6） |
| `logf` | 檔案物件 | **B** | — | 本局日誌檔，由 `open_log()` 開啟（§5.6） |

> 最後兩個欄位是 11/09 推骨架時補的，原本的表沒有。`conns` 是因為
> §5.3-6 要求「一局結束後關閉該局所有連線」——連那些連上來卻從沒送
> `HELLO` 的也要關，而那些連線不在 `players` 裡，B 讀不到。

## 4. Player 的欄位

| 欄位 | 寫 | 意義 |
|---|---|---|
| `pid` / `name` / `team` / `kind` / `conn` | **A**（建立時） | 基本資料 |
| `commit` | **A**（`on_commit`） | 承諾值，64 hex |
| `reveal` | **A**（`on_reveal`） | `(secret, salt)` |
| `status` | **B** | `LOBBY` → `ALIVE` → `GUESSED` / `FORFEIT` / `DQ`；或 `REMOVED` |
| `out_turn` | **B** | 離場回合，存活者為 `None` |
| `records` | **B** | `[(turn_id, guess, a, b)]`，供揭示驗證 |
| `cands` | **B** | 候選集合，初始 5040 個 |
| `skips` | **B** | 連續跳過次數 |

> **A 只碰 `commit` 與 `reveal` 兩個欄位**，其餘都是 B 的。

---

## 5. 三個雙寫欄位的規則

### `guess_open`

- **B 寫 `True`**：回合開始，開放喊數
- **A 寫 `False`**：收到合法喊數後關窗（防止同一回合重複送）
- **B 寫 `False`**：逾時關窗

> 規則：**只有 B 能開，兩邊都能關。**

### `feedback`

- **B 寫 `{}`**：回合開始時清空
- **A 寫 `feedback[pid] = (a, b)`**：收到合法回報時填入單一項目
- A **不可**整個重新賦值

### `reveal_wait`

- **B 寫 `set(pids)`**：送出 `REVEAL_REQUEST` 時填入
- **A 寫 `reveal_wait.discard(pid)`**：收到合法揭示時移除單一項目
- B 在收齊或逾時後清空

---

## 6. `changed.set()` 是鐵律

**唐偉捷每改一次狀態，都必須呼叫 `self.changed.set()`。**

漏一次，彭明山就會一路睡到逾時，整局卡 60 秒。這個 bug 不會報錯、只會「很慢」，極難查。

> **建議紀律：在每個 `on_*` 的最後一行無條件 `self.changed.set()`**，
> 不要去判斷「這次有沒有真的改到」。多敲鐘沒有任何壞處。

彭明山那邊對應的等待長這樣：

```python
async def wait_until(self, cond, timeout):
    """等 cond() 為真，或逾時。期間由 changed 事件喚醒重新檢查。"""
```

---

## 7. `broadcast()` 的歸屬

它是 `Server` 的方法，但兩邊都要用（A 的 `on_chat` 要轉播聊天）。

**彭明山寫，唐偉捷呼叫。** 簽名固定為：

```python
def broadcast(self, msg: dict) -> None: ...
```

廣播對象（§11.3）：所有**已完成 `HELLO` 且仍連線**的玩家，**包括已被猜中、棄權、
取消資格但仍在線上者**；只有在承諾階段被移出的玩家不在其內。

這個條件寫在 B 那邊一次就好，A 不用自己判斷。

---

## 8. 檔案怎麼切（避免 git 衝突）★

兩人同時改一個 `server.py` 必定衝突。用 **mixin** 拆開：

```python
# bcproto/server/net.py          ← 唐偉捷一個人改
class NetMixin:
    def dispatch(self, conn, msg): ...
    def on_hello(self, conn, m): ...
    def on_commit(self, conn, m): ...
    def on_guess(self, conn, m): ...
    def on_feedback(self, conn, m): ...
    def on_reveal(self, conn, m): ...
    def on_chat(self, conn, m): ...
    def on_ping(self, conn, m): ...
    async def handle_conn(self, reader, writer): ...


# bcproto/server/game.py         ← 彭明山一個人改
class GameMixin:
    async def run_game(self): ...
    async def wait_until(self, cond, timeout): ...
    async def collect_reveals(self, pids, reason): ...
    def broadcast(self, msg): ...
    def broadcast_lobby(self): ...
    def finish(self, reason, turns, ranking): ...


# bcproto/server/__init__.py     ← 幾乎不會動，這個檔就是契約本身
class Server(NetMixin, GameMixin):
    def __init__(self, args):
        self.a = args
        self.players = {}
        self.phase = "LOBBY"
        self.changed = asyncio.Event()
        self.next_id = 1
        self.turn_id = 0
        self.guesser = None
        self.guess_open = False
        self.cur_guess = None
        self.fb_open = False
        self.responders = []
        self.feedback = {}
        self.reveal_wait = set()
        ...
```

兩個檔案各自獨立，**git 永遠不會衝突**。狀態欄位集中在 `__init__` 一處——誰想加欄位就得改這個檔，自然會注意到要通知對方。

---

## 9. 唐偉捷的速查表

每個 handler 要檢查什麼、依什麼順序、讀哪些欄位、寫哪些欄位。
**檢查順序是規範 §13 規定的，不可調換**——同一則訊息犯多條時只回第一個符合的錯誤碼。

### `on_hello`

| 順序 | 檢查 | 讀 | 不符時 |
|---|---|---|---|
| 1 | `protocol`/`name`/`team`/`kind` 四個字串欄位齊全 | — | `BAD_FIELD`（致命） |
| 2 | `protocol` 符合 `BC/1\.[0-9]+` | — | `VERSION_UNSUPPORTED`（致命） |
| 3 | `name`/`team` 1–20 字無控制字元、`kind` ∈ {human, ai} | — | `INVALID_VALUE`（致命） |
| 4 | 在大廳階段 | `phase` | `GAME_IN_PROGRESS`（致命） |
| 5 | 人數未滿 | `len(players)` | `ROOM_FULL`（致命） |
| 6 | 名稱未重複 | `players` | `NAME_TAKEN`（致命） |

通過後：指派 `pid = f"p{next_id}"`、`next_id += 1`、建 `Player`、送 `WELCOME`、
呼叫 `broadcast_lobby()`、`changed.set()`。

> 已完成 `HELLO` 的連線**再次**送 `HELLO` → **非致命** `UNEXPECTED`，連線保留。

### `on_commit`

| 順序 | 檢查 | 讀 | 不符時 |
|---|---|---|---|
| 1 | `commit` 是字串 | — | `BAD_FIELD` |
| 2 | `phase == "COMMIT"` 且 `p.status == "ALIVE"` | `phase` | `UNEXPECTED` |
| 3 | `p.commit is None` | `p.commit` | `DUPLICATE` |
| 4 | 64 個小寫十六進位字元 | — | `INVALID_VALUE` |

通過後：`p.commit = c`、`changed.set()`。

### `on_guess`

| 順序 | 檢查 | 讀 | 不符時 |
|---|---|---|---|
| 1 | `turn_id` 是整數（**排除 bool**）、`guess` 是字串 | — | `BAD_FIELD` |
| 2 | `phase == "PLAY"` | `phase` | `UNEXPECTED` |
| 3 | `turn_id` 等於目前回合 | `turn_id` | `STALE_TURN` |
| 4 | 送出者是喊數者 | `guesser` | `NOT_YOUR_TURN` |
| 5 | 窗還開著 | `guess_open` | `UNEXPECTED` |
| 6 | 四個互異數字 | — | `INVALID_VALUE` |

通過後：`cur_guess = g`、`guess_open = False`、`changed.set()`。

### `on_feedback`

| 順序 | 檢查 | 讀 | 不符時 |
|---|---|---|---|
| 1 | `turn_id`/`a`/`b` 都是整數（**排除 bool**） | — | `BAD_FIELD` |
| 2 | `phase == "PLAY"` | `phase` | `UNEXPECTED` |
| 3 | `turn_id` 等於目前回合 | `turn_id` | `STALE_TURN` |
| 4 | 窗開著且自己在回報者名單 | `fb_open`、`responders` | `UNEXPECTED` |
| 5 | 尚未回報過 | `feedback` | `DUPLICATE` |
| 6 | `(a, b)` 在 14 種合法組合內（**3A1B 不合法**） | — | `INVALID_VALUE` |

通過後：`feedback[pid] = (a, b)`、`changed.set()`。

### `on_reveal`

| 順序 | 檢查 | 讀 | 不符時 |
|---|---|---|---|
| 1 | `secret`/`salt` 都是字串 | — | `BAD_FIELD` |
| 2 | 自己在等待揭示名單中 | `reveal_wait`、`p.reveal` | 已揭示過 → `DUPLICATE`；不需揭示 → `UNEXPECTED` |
| 3 | `secret` 四位互異、`salt` 32 個小寫 hex | — | `INVALID_VALUE` |

通過後：`p.reveal = (s, salt)`、`reveal_wait.discard(pid)`、`changed.set()`。

> 格式正確但**內容不實**的 `REVEAL`（秘密與承諾值不符）**不是協定錯誤**，
> 由彭明山在驗證階段判成 `BAD_COMMIT`。A 這裡只檢查格式。

### `on_chat`

`BAD_FIELD`（`text` 不是字串）→ `INVALID_VALUE`（不是 1–200 字或含控制字元）
→ 通過則 `broadcast({"type": "CHAT", "from": pid, "text": text})`。

### 其他

- `on_ping`：回 `PONG`，`PING` 帶 `nonce` 時**必須原樣回傳**
- `on_pong`：忽略
- `on_bye`：`conn.close()`，效果等同斷線
- 不認識的類型：`UNKNOWN_TYPE`（非致命）

---

## 10. `fatal` 旗標要注意

同一個錯誤碼在不同情境下致命與否會變：

| 錯誤碼 | 致命條件 |
|---|---|
| `BAD_FIELD` | **只有在 `HELLO` 時致命** |
| `INVALID_VALUE` | **只有在 `HELLO` 時致命** |
| `UNEXPECTED` | **只有在 `HELLO` 之前致命** |
| `LINE_TOO_LONG`、`VERSION_UNSUPPORTED`、`GAME_IN_PROGRESS`、`ROOM_FULL`、`NAME_TAKEN`、`TIMEOUT`、`TOO_MANY_ERRORS` | 一律致命 |
| `BAD_JSON`、`UNKNOWN_TYPE`、`NOT_YOUR_TURN`、`STALE_TURN`、`DUPLICATE` | 一律非致命 |

另有一個特例：同一連線累積**超過 20 次**錯誤時，第 21 次要**先送出該次的錯誤，
再送致命的 `TOO_MANY_ERRORS`** 然後斷線。這是「一則訊息只回應一則 `ERROR`」的唯一例外。

---

## 11. 日誌：A 要呼叫，B 已經實作好

規範 §5.6 要求伺服器把**每一則**收送的訊息寫進 `--log-dir` 的 JSON Lines 檔，
一局一檔、每筆立即 flush。收送的動作都發生在協定層，所以**呼叫點在 A 這邊**，
但函式彭明山已經寫好了，唐偉捷只要在兩個地方各加一行：

```python
# Conn.send() 裡，放在真正丟進傳送佇列之前
self.srv.log("send", self.label(), msg)

# handle_conn() 收到一行、decode_line() 成功之後
self.log("recv", conn.label(), msg)

# decode_line() 回 BAD_JSON 時——無法解析的輸入也要記
self.log_raw("recv", conn.label(), text)
```

`label()` 是 A 的 `Conn` 方法，規則是：**完成 `HELLO` 後回 `player_id`，之前回
`IP:埠號`**。這條是規範規定的 `peer` 欄位格式，不要自己改。

`logf` 還沒開（大廳尚未開放）時 `log()` 會直接忽略，不會拋例外，所以不用防。

---

## 12. 待決議

```
☑ 欄位名稱沿用本文           → 已推成 bcproto/server/__init__.py
☑ 檔案切法採 mixin 三檔       → net.py（唐）／game.py（彭）／__init__.py（契約）
☑ broadcast(msg: dict) -> None 由彭明山實作  → 已完成，含 §11.3 的對象判定
☐ guess_open / feedback / reveal_wait 的雙寫規則確認
☐ changed.set() 紀律：每個 on_* 最後無條件呼叫
☐ 新增的 conns 與日誌呼叫點（見 §3、§11）確認
```

**骨架已經推上去了**（11/09）。`bcproto/server/net.py` 是唐偉捷的檔案，裡面每個
`on_*` 都只有 docstring 加 `raise NotImplementedError`，docstring 就是 §9 的檢查
順序表。把 `raise` 換成實作即可，不必等遊戲流程寫完。

`bcproto/server/game.py` 目前完成 `broadcast()`、`broadcast_lobby()`、
`wait_until()`、`close_all_conns()` 與日誌五個函式；`run_game()` 的五階段流程
要等 A10／A11 能讓玩家真的加入才能接上。
