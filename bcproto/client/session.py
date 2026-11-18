"""人類玩家的協定狀態與 C12 回報流程。"""

from __future__ import annotations

import secrets

from ..messages import commit, feedback, reveal
from ..rules import make_commit, score
from ..validators import valid_feedback, valid_number


class ClientSession:
    """保存本局秘密，並處理預填回報與修改確認。"""

    def __init__(self):
        self.secret: str | None = None
        self.salt: str | None = None
        self.commit: str | None = None
        self._feedback_drafts: dict[int, tuple[str, int, int]] = {}

    def set_secret(self, secret: str) -> dict:
        """設定秘密並建立 COMMIT；秘密必須是四位不重複 ASCII 數字。"""
        if not valid_number(secret):
            raise ValueError("秘密必須是四位不重複 ASCII 數字")
        self.secret = secret
        self.salt = secrets.token_hex(16)
        self.commit = make_commit(secret, self.salt)
        return commit(self.commit)

    def reveal_message(self) -> dict:
        """建立 REVEAL；尚未設定秘密時拒絕送出。"""
        if self.secret is None or self.salt is None:
            raise ValueError("尚未設定秘密")
        return reveal(self.secret, self.salt)

    def prepare_feedback(self, turn_id: int, guess: str) -> dict:
        """依自己的秘密預填 FEEDBACK，並保存草稿供畫面修改。"""
        if self.secret is None:
            raise ValueError("尚未設定秘密")
        if not valid_number(guess):
            raise ValueError("對方喊數格式錯誤")
        a, b = score(guess, self.secret)
        self._feedback_drafts[turn_id] = (guess, a, b)
        return feedback(turn_id, a, b)

    def submit_feedback(
        self,
        turn_id: int,
        a: int | None = None,
        b: int | None = None,
        *,
        confirmed: bool = False,
    ) -> dict:
        """送出預填或修改後的回報。

        若玩家修改了預填值，必須以 ``confirmed=True`` 二次確認。
        """
        draft = self._feedback_drafts.get(turn_id)
        if draft is None:
            raise ValueError("找不到該回合的回報草稿")
        _, expected_a, expected_b = draft
        actual_a = expected_a if a is None else a
        actual_b = expected_b if b is None else b
        if not valid_feedback(actual_a, actual_b):
            raise ValueError("FEEDBACK 的 (a,b) 不合法")
        changed = (actual_a, actual_b) != (expected_a, expected_b)
        if changed and not confirmed:
            raise ValueError("修改自動計算結果需要二次確認")
        self._feedback_drafts.pop(turn_id, None)
        return feedback(turn_id, actual_a, actual_b)



