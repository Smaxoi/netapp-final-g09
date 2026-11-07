"""遊戲規則與公平機制的純函式。

本模組不碰網路、不碰狀態，只做運算，因此可以單獨測試。
server 用它判定回合結果與名次，client 與 ai 用它推論對手的秘密數字並自行驗證
伺服器的判定（規範 §3.2 要求客戶端「應自行驗證伺服器廣播的每一筆揭示結果」）。

對應規範：§2.1 秘密數字、§2.2 幾A幾B、§2.4 名次、§3.1 承諾值、
         §14.1 公開一致性檢查、§14.2 揭示驗證、§14.3 名次計算。
"""
from __future__ import annotations

import hashlib
import itertools

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------

# §2.1：秘密數字與喊數都是四個 ASCII 數字字元，四個位數兩兩相異，允許首位為 0。
# 合法數字共 10 × 9 × 8 × 7 = 5040 個。
ALL_NUMBERS: list[str] = ["".join(p) for p in itertools.permutations("0123456789", 4)]

# §2.2：合法回報必須滿足 a + b <= 4；且不可能出現 3A1B——三個位置已對時，
# 第四個數字若也在秘密數字中，只能落在剩下的位置而成為 4A。因此恰有 14 種。
VALID_FEEDBACK: frozenset[tuple[int, int]] = frozenset(
    (a, b)
    for a in range(5)
    for b in range(5)
    if a + b <= 4 and (a, b) != (3, 1)
)


# ---------------------------------------------------------------------------
# §2.2 幾A幾B
# ---------------------------------------------------------------------------
def score(guess: str, secret: str) -> tuple[int, int]:
    """計算喊數 guess 對秘密數字 secret 的 (A, B)。

    A = 數字與位置皆正確的個數
    B = 數字正確但位置不對的個數
      = |{guess 的數字} ∩ {secret 的數字}| − A

    因為四個位數兩兩相異，用集合交集算共同數字不會重複計數。

    >>> score("0927", "0275")      # §2.2 的例子：0 位置正確，2 與 7 存在但位置不對
    (1, 2)
    """
    a = sum(g == s for g, s in zip(guess, secret))
    b = len(set(guess) & set(secret)) - a
    return a, b


# ---------------------------------------------------------------------------
# §14.1 公開一致性檢查
# ---------------------------------------------------------------------------
def filter_candidates(cands: list[str], guess: str, a: int, b: int) -> list[str]:
    """從候選集合中保留與回報 (a, b) 相容的秘密數字。

    伺服器為每位玩家維護一個初始為全部 5040 個的候選集合，每收到一筆回報就刪減一次。
    若集合變成空的，代表不存在任何秘密數字能解釋該玩家的全部回報，也就證明他至少
    說謊過一次，當回合立即取消資格（§3.2 第一道防線）。

    AI 用完全相同的運算推論對手的秘密——差別只在於伺服器用它抓作弊，AI 用它縮小搜尋範圍。
    """
    return [s for s in cands if score(guess, s) == (a, b)]


# ---------------------------------------------------------------------------
# §3.1 承諾值
# ---------------------------------------------------------------------------
def make_commit(secret: str, salt: str) -> str:
    """c = SHA256(secret + ":" + salt)，回傳 64 個小寫十六進位字元。

    鹽值必須由呼叫端以 secrets.token_hex(16) 產生，不可用 random——random 的輸出
    可被預測（§16.3 陷阱 7）。鹽值的作用是「隱藏性」：合法秘密只有 5040 個，若只對
    秘密本身取雜湊，對手把 5040 個雜湊算一遍就能反查；加入 128 位元隨機鹽值後，
    窮舉變得不可行。SHA-256 的抗碰撞性則提供「綁定性」，讓玩家無法事後換一組
    秘密與鹽值來符合同一個承諾值。
    """
    return hashlib.sha256(f"{secret}:{salt}".encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# §14.2 揭示驗證
# ---------------------------------------------------------------------------
def verify_reveal(
    commit: str | None,
    records: list[tuple[int, str, int, int]],
    secret: str | None,
    salt: str | None,
) -> tuple[str, list[int]]:
    """驗證一筆揭示，回傳 (verdict, bad_turns)。

    參數
        commit  : 該玩家開局時送出的承諾值
        records : 他的公開回報紀錄 [(turn_id, guess, a, b), ...]
        secret  : 揭示的秘密數字；未在時限內收到合法 REVEAL 時為 None
        salt    : 揭示的鹽值；同上

    判定依 §14.2 的四個步驟，順序不可調換：
        1. 沒有揭示            → NO_REVEAL
        2. 承諾值對不上        → BAD_COMMIT
        3. 歷史回報重算不符    → INCONSISTENT，bad_turns 列出不符的回合編號
        4. 其餘                → OK

    步驟 3 同時涵蓋兩種作弊：被淘汰者的秘密必須等於猜中他的那個喊數（只有 g = s 時
    才會得到 4A0B）；存活者則不可能曾經隱瞞過 4A。

    bad_turns 只在 INCONSISTENT 時非空，其餘判定一律回傳空陣列（§11.3）。
    """
    if secret is None or salt is None:
        return "NO_REVEAL", []
    if make_commit(secret, salt) != commit:
        return "BAD_COMMIT", []
    bad = [turn_id for (turn_id, guess, a, b) in records if score(guess, secret) != (a, b)]
    return ("INCONSISTENT", bad) if bad else ("OK", [])


# ---------------------------------------------------------------------------
# §14.3 名次計算
# ---------------------------------------------------------------------------
# 終局狀態的排序分類：數字越小名次越前（§2.4）。
_CATEGORY = {"SURVIVOR": 0, "GUESSED": 1, "FORFEIT": 2}


def _rank_key(entry: tuple[str, str, int | None]) -> tuple[int, int]:
    """排序鍵：(類別, −離場回合)。

    同一類別中「離場回合越晚名次越前」，所以把 out_turn 取負號後遞增排序。
    存活者沒有離場回合，固定用 0。
    """
    _pid, status, out_turn = entry
    return (_CATEGORY[status], 0 if status == "SURVIVOR" else -out_turn)


def compute_ranking(
    entries: list[tuple[str, str, int | None]],
) -> list[tuple[str, str, int | None, int | None]]:
    """計算最終名次，回傳 [(pid, status, out_turn, rank), ...]。

    參數 entries 的每一項是 (player_id, status, out_turn)，
    status ∈ SURVIVOR / GUESSED / FORFEIT / DQ。

    規則（§2.4、§14.3）：
        1. 取消資格（DQ）者先行排除，附在最後且 rank 為 None。
        2. 其餘依排序鍵遞增排序。
        3. 名次 = 1 + 「鍵嚴格小於自己的玩家數」，因此鍵相同者並列，
           下一名次跳號（標準競賽排名 1、2、2、4）。

    例（§2.4）：四人對局，甲存活到最後，乙與丙同在第 9 回合被猜中，丁在第 12 回合
    斷線 → 甲 1、乙 2、丙 2、丁 4。丁雖然較晚離場，但棄權者的回報無法經揭示驗證，
    故排在被猜中者之後；這也讓「故意斷線以逃避驗證」無利可圖。
    """
    ranked = sorted((e for e in entries if e[1] != "DQ"), key=_rank_key)
    keys = [_rank_key(e) for e in ranked]
    rows = [
        (pid, status, out_turn, 1 + sum(k < _rank_key((pid, status, out_turn)) for k in keys))
        for (pid, status, out_turn) in ranked
    ]
    return rows + [(pid, st, t, None) for (pid, st, t) in entries if st == "DQ"]
