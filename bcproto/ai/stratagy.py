"""AI 喊數策略。

策略函式保持無網路狀態，讓 agent 只負責處理協定事件。
"""

from __future__ import annotations

import random

_MAX_GUESSES_TO_EVALUATE = 256

try:
    from bcproto.rules import ALL_NUMBERS, score
except ImportError:
    from rules import ALL_NUMBERS, score


def choose_guess(candidate_sets: dict[str, list[str]] | None = None) -> str:
    """C10 most-parts 策略。

    鎖定候選最少的對手，從該候選集合中挑選能產生最多種
    (A, B) 分割的喊數。同分時選擇最大分組較小的喊數。
    """
    if candidate_sets:
        nonempty = [cands for cands in candidate_sets.values() if cands]
        if nonempty:
            target = min(nonempty, key=len)
            best_guess = target[0]
            best_key = (-1, -len(target))

            if len(target) <= _MAX_GUESSES_TO_EVALUATE:
                guess_pool = target
            else:
                # 初始集合有 5040 組；均勻取樣避免第一回合做 25M 次比較。
                step = len(target) / _MAX_GUESSES_TO_EVALUATE
                guess_pool = [target[int(i * step)] for i in range(_MAX_GUESSES_TO_EVALUATE)]

            for guess in guess_pool:
                partitions: dict[tuple[int, int], int] = {}
                for secret in target:
                    result = score(guess, secret)
                    partitions[result] = partitions.get(result, 0) + 1

                # 最大化分割數；同分時最小化最大分組大小。
                key = (len(partitions), -max(partitions.values()))
                if key > best_key:
                    best_key = key
                    best_guess = guess
            return best_guess
    return random.choice(ALL_NUMBERS)
