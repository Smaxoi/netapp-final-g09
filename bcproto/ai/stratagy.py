"""AI 喊數策略。

策略函式保持無網路狀態，讓 agent 只負責處理協定事件。
"""

from __future__ import annotations

import random

try:
    from bcproto.rules import ALL_NUMBERS
except ImportError:
    from rules import ALL_NUMBERS


def choose_guess(candidate_sets: dict[str, list[str]] | None = None) -> str:
    """優先猜候選最少的對手；尚未有候選資料時隨機挑合法數字。"""
    if candidate_sets:
        nonempty = [cands for cands in candidate_sets.values() if cands]
        if nonempty:
            smallest = min(nonempty, key=len)
            return random.choice(smallest)
    return random.choice(ALL_NUMBERS)
