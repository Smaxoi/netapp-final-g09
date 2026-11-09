"""B8：`Server` 與 `Player` 的狀態欄位——這個檔就是《Server 狀態契約》本身。

協定層（唐偉捷，`net.py`）與遊戲引擎（彭明山，`game.py`）會同時操作同一個
`Server` 物件，但跑在不同的 asyncio task 裡：

    handle_conn（每條連線一個 task）          run_game（一個 task）
        讀 socket → 驗證 → 改欄位 → 敲鐘   │   開窗 → await 等 → 讀欄位 → 廣播
                        ↓                  │             ↑
        └──────────── 同一個 Server 物件 ───┘

協定層不能直接呼叫遊戲流程，因為後者正卡在 `await` 上。唯一的溝通方式是
**改欄位 → `changed.set()` → 另一邊醒來讀欄位**。欄位的讀寫權責寫在下面每個
欄位的註解裡，三個「雙寫」欄位另有明確規則，不要自行擴張。

用 mixin 把兩人的程式拆成兩個檔案，git 永遠不會衝突；狀態欄位集中在這裡一處，
誰要加欄位就得改這個檔，自然會注意到必須通知對方。
"""

import asyncio

from ..rules import ALL_NUMBERS
from .game import GameMixin
from .net import NetMixin

#: 一條連線累積超過 20 次錯誤，第 21 次要先送該錯誤再送致命的
#: `TOO_MANY_ERRORS`（§13）。放在這裡是因為 A 與 B 都可能需要讀它。
MAX_ERRORS = 20

#: 傳送佇列上限。單一連線慢或惡意不得拖累其他玩家（§5.3-1），
#: 佇列積太多就直接斷該連線。
MAX_OUTQUEUE = 256

#: 完成 `HELLO` 的時限（§13 `TIMEOUT`，致命）。
HELLO_TIMEOUT = 10.0


class Player:
    """一位本局玩家。

    協定層只負責建立物件，以及之後填 `commit` 與 `reveal` 兩個欄位；
    其餘欄位全部由遊戲引擎維護。
    """

    __slots__ = ("pid", "name", "team", "kind", "conn",
                 "commit", "reveal", "status", "out_turn",
                 "records", "cands", "skips")

    def __init__(self, pid: str, name: str, team: str, kind: str, conn):
        # ---- 基本資料：協定層在 on_hello 通過後建立，之後不再變動 ----
        self.pid = pid                  # 伺服器指派，`p` + 正整數，每局自 p1 起編（§9）
        self.name = name                # 1–20 字，同局不可重複
        self.team = team                # 組別代碼
        self.kind = kind                # "human" 或 "ai"
        self.conn = conn                # 這位玩家的連線物件（A 的 Conn）

        # ---- 協定層填寫（只有這兩個）----
        self.commit: str | None = None               # 64 個小寫 hex（§3.1）
        self.reveal: tuple[str, str] | None = None   # (secret, salt)

        # ---- 以下全部由遊戲引擎維護 ----
        # LOBBY → ALIVE → GUESSED / FORFEIT / DQ；承諾階段未承諾者為 REMOVED
        self.status = "LOBBY"
        self.out_turn: int | None = None   # 離場回合；存活到終局者為 None

        # 這位玩家歷史上送出的每一則回報，供揭示時重算一致性（§3.3）
        self.records: list[tuple[int, str, int, int]] = []

        # 伺服器對這位玩家秘密數字的公開候選集合：初始 5040 個，
        # 每回合依其回報刪減；變成空集合即當場取消資格（§3.2 第一道防線）。
        self.cands: list[str] = list(ALL_NUMBERS)

        # 連續逾時未喊數的次數，達 --max-skips 視為棄權
        self.skips = 0

    @property
    def joined(self) -> bool:
        """是否已完成 HELLO 並仍屬於本局（承諾階段被移出者不算）。"""
        return self.status != "REMOVED"

    def __repr__(self) -> str:
        return f"<Player {self.pid} {self.name!r} {self.kind} {self.status}>"


class Server(NetMixin, GameMixin):
    """BC/1.0 伺服器。

    `NetMixin` 由唐偉捷實作（`net.py`），`GameMixin` 由彭明山實作（`game.py`）。
    兩個 mixin 都不自己存狀態，狀態一律是這裡 `__init__` 建立的欄位。
    """

    def __init__(self, args):
        # argparse 的結果。§5.2 規定的選項名稱與預設值不得更改。
        self.a = args

        # ---- 階段與回合：B 寫、A 讀 ----
        self.phase = "LOBBY"            # LOBBY / COMMIT / PLAY / END / OVER
        self.turn_id = 0                # 目前回合，自 1 起
        self.guesser: str | None = None  # 本回合喊數者的 pid

        # ---- 喊數窗 ----
        # guess_open 是雙寫欄位：只有 B 能開（回合開始），兩邊都能關
        # （A 收到合法喊數後關窗防重送，B 逾時關窗）。
        self.guess_open = False
        self.cur_guess: str | None = None   # A 填、B 讀

        # ---- 回報窗 ----
        self.fb_open = False                 # B 寫、A 讀
        self.responders: list[str] = []       # B 寫、A 讀；= 存活者去掉喊數者
        # feedback 是雙寫欄位：B 在回合開始時整個清成 {}，
        # A 只能填單一項目 feedback[pid] = (a, b)，不可整個重新賦值。
        self.feedback: dict[str, tuple[int, int]] = {}

        # ---- 揭示窗 ----
        # reveal_wait 是雙寫欄位：B 送出 REVEAL_REQUEST 時填入整個集合，
        # A 收到合法揭示時只能 discard(pid)，B 在收齊或逾時後清空。
        self.reveal_wait: set[str] = set()

        # ---- 玩家 ----
        self.players: dict[str, Player] = {}   # A 新增、B 改狀態
        self.next_id = 1                        # A 寫；每局自 1 起編
        # 所有連線，含尚未完成 HELLO 者。A 在 accept／斷線時增刪，
        # B 只讀——局末要關閉本局所有連線（§5.3-6）。
        self.conns: set = set()

        # ---- 跨 task 的喚醒訊號 ----
        # 鐵律：協定層每個 on_* 的最後一行無條件 self.changed.set()。
        # 漏一次，遊戲迴圈會一路睡到逾時，整局卡住且不會報錯。
        self.changed = asyncio.Event()

        # ---- 本局識別與日誌（B16）----
        self.game_id: str | None = None
        self.logf = None                # 由 open_log() 開啟，一局一檔（§5.6）

        # ---- 莊家主控台 ----
        # 只有在標準輸入是終端機時才讀主控台命令（§5.3-8）。
        self.manual_start = False

        # ---- --bots 子行程（B17）與 --games 迴圈（B18）----
        self.bot_procs: list = []
        self.games_played = 0


__all__ = ["Server", "Player", "MAX_ERRORS", "MAX_OUTQUEUE", "HELLO_TIMEOUT"]
