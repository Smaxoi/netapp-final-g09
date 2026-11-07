"""bcproto.rules 的單元測試。

測試資料全部取自規範本身，不是自己編的：
    §2.2  score("0927", "0275") == (1, 2)
    §3.1  SHA-256 測試向量
    §2.4  四人對局的名次例子（甲1、乙2、丙2、丁4）

兩種跑法都可以：
    python tests/test_rules.py        （不需要任何第三方套件）
    pytest tests/test_rules.py
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bcproto.rules import (  # noqa: E402
    ALL_NUMBERS,
    VALID_FEEDBACK,
    compute_ranking,
    filter_candidates,
    make_commit,
    score,
    verify_reveal,
)

# 規範 §3.1 的測試向量
SPEC_SECRET = "0275"
SPEC_SALT = "2eb6594c74b9b9d71e2d9b5bd14300df"
SPEC_COMMIT = "6b05c8fe4113aa24833fcbd089d33f0fe48ff68eb0a7d7fff87319a394338721"


# ---------------------------------------------------------------------------
# §2.1 合法數字
# ---------------------------------------------------------------------------
def test_all_numbers_count():
    """10 × 9 × 8 × 7 = 5040 個，且每個都是四位相異數字。"""
    assert len(ALL_NUMBERS) == 5040
    assert len(set(ALL_NUMBERS)) == 5040
    assert all(len(n) == 4 and len(set(n)) == 4 and n.isdigit() for n in ALL_NUMBERS)
    assert "0275" in ALL_NUMBERS          # 允許首位為 0


# ---------------------------------------------------------------------------
# §2.2 幾A幾B
# ---------------------------------------------------------------------------
def test_score_spec_example():
    """秘密 0275、喊數 0927 → 1A2B（0 位置正確，2 與 7 存在但位置不對）。"""
    assert score("0927", "0275") == (1, 2)


def test_score_exact_match():
    assert score("0275", "0275") == (4, 0)


def test_score_no_common_digit():
    assert score("1348", "0275") == (0, 0)


def test_score_all_digits_wrong_position():
    """四個數字都對、位置全錯 → 0A4B。"""
    assert score("2750", "0275") == (0, 4)


def test_score_is_always_valid_feedback():
    """誠實回報必然落在 14 種合法組合之內——隨機抽 300 對驗證。"""
    rng = random.Random(20261107)
    for _ in range(300):
        guess, secret = rng.choice(ALL_NUMBERS), rng.choice(ALL_NUMBERS)
        assert score(guess, secret) in VALID_FEEDBACK


def test_valid_feedback_has_14_and_excludes_3a1b():
    """恰有 14 種，且 3A1B 不可能出現。"""
    assert len(VALID_FEEDBACK) == 14
    assert (3, 1) not in VALID_FEEDBACK
    assert all(a + b <= 4 for a, b in VALID_FEEDBACK)
    assert (4, 0) in VALID_FEEDBACK and (0, 0) in VALID_FEEDBACK


# ---------------------------------------------------------------------------
# §14.1 候選集合刪減
# ---------------------------------------------------------------------------
def test_filter_keeps_the_true_secret():
    """誠實回報之後，真正的秘密一定還留在候選集合裡。"""
    rng = random.Random(42)
    secret = rng.choice(ALL_NUMBERS)
    cands = list(ALL_NUMBERS)
    for _ in range(5):
        guess = rng.choice(ALL_NUMBERS)
        a, b = score(guess, secret)
        cands = filter_candidates(cands, guess, a, b)
        assert secret in cands
    assert len(cands) < 5040          # 確實有刪減


def test_filter_detects_liar():
    """永遠回報 0A0B 的說謊者，候選集合遲早變成空集。"""
    cands = list(ALL_NUMBERS)
    rng = random.Random(7)
    for _ in range(40):
        cands = filter_candidates(cands, rng.choice(ALL_NUMBERS), 0, 0)
        if not cands:
            break
    assert cands == []


# ---------------------------------------------------------------------------
# §3.1 承諾值
# ---------------------------------------------------------------------------
def test_make_commit_spec_vector():
    """規範 §3.1 給的測試向量。"""
    assert make_commit(SPEC_SECRET, SPEC_SALT) == SPEC_COMMIT


def test_make_commit_format():
    """64 個小寫十六進位字元。"""
    c = make_commit("1234", "0" * 32)
    assert len(c) == 64
    assert all(ch in "0123456789abcdef" for ch in c)


def test_make_commit_salt_changes_result():
    """同一個秘密配不同鹽值要得到不同承諾值，否則就沒有隱藏性。"""
    assert make_commit("0275", "a" * 32) != make_commit("0275", "b" * 32)


# ---------------------------------------------------------------------------
# §14.2 揭示驗證
# ---------------------------------------------------------------------------
def test_verify_ok():
    records = [(1, "0927", 1, 2), (2, "0123", 1, 1)]
    assert verify_reveal(SPEC_COMMIT, records, SPEC_SECRET, SPEC_SALT) == ("OK", [])


def test_verify_no_reveal():
    """未在時限內收到合法 REVEAL。"""
    assert verify_reveal(SPEC_COMMIT, [], None, None) == ("NO_REVEAL", [])
    assert verify_reveal(SPEC_COMMIT, [], SPEC_SECRET, None) == ("NO_REVEAL", [])


def test_verify_bad_commit():
    """揭示的秘密與承諾值不符——例如想用另一個數字圓謊。"""
    verdict, bad = verify_reveal(SPEC_COMMIT, [], "1348", SPEC_SALT)
    assert verdict == "BAD_COMMIT"
    assert bad == []                   # 非 INCONSISTENT 時一律空陣列（§11.3）


def test_verify_inconsistent_lists_bad_turns():
    """一致地假裝另一個秘密：每回合檢查都過得了，終局揭示時被抓。"""
    records = [
        (1, "0927", 1, 2),             # 對 0275 而言正確
        (3, "0123", 0, 0),             # 錯：score("0123", "0275") == (1, 1)
        (5, "1348", 0, 0),             # 對 0275 而言正確
        (7, "2750", 1, 1),             # 錯：score("2750", "0275") == (0, 4)
    ]
    verdict, bad = verify_reveal(SPEC_COMMIT, records, SPEC_SECRET, SPEC_SALT)
    assert verdict == "INCONSISTENT"
    assert bad == [3, 7]               # 只列不符的回合編號


# ---------------------------------------------------------------------------
# §2.4 / §14.3 名次
# ---------------------------------------------------------------------------
def test_ranking_spec_example():
    """§2.4 的例子：甲存活、乙丙同在第 9 回合被猜中、丁第 12 回合斷線。

    期望 甲1、乙2、丙2、丁4——並列者同名次，下一名次跳號。
    丁雖然較晚離場，但棄權排在所有被猜中者之後。
    """
    entries = [
        ("甲", "SURVIVOR", None),
        ("乙", "GUESSED", 9),
        ("丙", "GUESSED", 9),
        ("丁", "FORFEIT", 12),
    ]
    assert {pid: rank for pid, _st, _t, rank in compute_ranking(entries)} == {
        "甲": 1, "乙": 2, "丙": 2, "丁": 4
    }


def test_ranking_later_elimination_ranks_higher():
    """同為 GUESSED 時，淘汰回合越晚名次越前。"""
    entries = [("p1", "GUESSED", 3), ("p2", "GUESSED", 7), ("p3", "SURVIVOR", None)]
    ranks = {pid: rank for pid, _st, _t, rank in compute_ranking(entries)}
    assert ranks["p3"] == 1
    assert ranks["p2"] == 2            # 第 7 回合離場
    assert ranks["p1"] == 3            # 第 3 回合離場，更早，名次更後


def test_ranking_dq_has_no_rank_and_goes_last():
    """取消資格者 rank 為 None 且排在最後。"""
    entries = [("p1", "DQ", 4), ("p2", "SURVIVOR", None), ("p3", "GUESSED", 6)]
    rows = compute_ranking(entries)
    assert rows[-1][0] == "p1"
    assert rows[-1][3] is None
    assert {pid: rank for pid, _st, _t, rank in rows if rank is not None} == {"p2": 1, "p3": 2}


def test_ranking_all_survivors_tie_at_first():
    """因回合上限而結束時，所有存活者並列第 1 名。"""
    entries = [("p1", "SURVIVOR", None), ("p2", "SURVIVOR", None), ("p3", "GUESSED", 5)]
    ranks = {pid: rank for pid, _st, _t, rank in compute_ranking(entries)}
    assert ranks["p1"] == ranks["p2"] == 1
    assert ranks["p3"] == 3            # 跳過第 2 名


# ---------------------------------------------------------------------------
# 不依賴 pytest 的執行入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as ex:                     # noqa: PERF203
            failed += 1
            print(f"  [FAIL] {name}  {ex}")
    print(f"\n通過 {len(tests) - failed} 項，失敗 {failed} 項（共 {len(tests)} 項）")
    sys.exit(1 if failed else 0)
