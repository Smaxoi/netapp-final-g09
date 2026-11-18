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


class ClientDisplay:
    """C13：保存並呈現 server 廣播的對局資訊。"""

    def __init__(self):
        self.phase = "LOBBY"
        self.lobby_players: list[dict] = []
        self.order: list[str] = []
        self.alive: list[str] = []
        self.turn_id: int | None = None
        self.guesser: str | None = None
        self.current_guess: str | None = None
        self.responders: list[str] = []
        self.turn_results: list[dict] = []
        self.eliminated: list[str] = []
        self.forfeited: list[str] = []
        self.disqualified: list[str] = []
        self.reveals: list[dict] = []
        self.ranking: list[dict] = []
        self.events: list[str] = []

    def handle(self, msg: dict) -> None:
        """接收一則 server 訊息並更新畫面狀態。"""
        kind = msg.get("type")
        if kind == "LOBBY":
            self.phase = "LOBBY"
            self.lobby_players = list(msg.get("players", []))
            self.events.append(f"大廳：{len(self.lobby_players)} 位玩家")
        elif kind == "GAME_START":
            self.phase = "COMMIT"
            self.order = list(msg.get("order", []))
            self.events.append(f"遊戲開始，輪轉順序：{' → '.join(self.order)}")
        elif kind == "TURN":
            self.phase = "PLAY"
            self.turn_id = msg.get("turn_id")
            self.guesser = msg.get("guesser")
            self.alive = list(msg.get("alive", []))
            self.current_guess = None
            self.responders = []
            self.events.append(f"第 {self.turn_id} 回合：{self.guesser} 喊數")
        elif kind == "GUESS_MADE":
            self.current_guess = msg.get("guess")
            self.responders = list(msg.get("responders", []))
            self.events.append(
                f"{msg.get('guesser')} 猜 {self.current_guess}，回報者：{', '.join(self.responders)}"
            )
        elif kind == "TURN_RESULT":
            result = dict(msg)
            self.turn_results.append(result)
            self.eliminated.extend(msg.get("eliminated", []))
            self.forfeited.extend(msg.get("forfeited", []))
            self.disqualified.extend(msg.get("disqualified", []))
            self.events.append(
                f"第 {msg.get('turn_id')} 回合判定：{self._format_results(msg.get('results', []))}"
            )
        elif kind == "REVEALED":
            self.reveals.append(dict(msg))
            self.events.append(
                f"揭示判定：{msg.get('player')} → {msg.get('verdict')}"
            )
        elif kind == "GAME_OVER":
            self.phase = "OVER"
            self.ranking = list(msg.get("ranking", []))
            self.events.append(f"遊戲結束：{msg.get('reason')}")

    @staticmethod
    def _format_results(results: list[dict]) -> str:
        return ", ".join(
            f"{row.get('player', row.get('player_id'))} {row.get('a')}A{row.get('b')}B"
            for row in results
        ) or "無回報"

    def render(self) -> str:
        """回傳可直接印到終端機的完整資訊摘要。"""
        lines = [f"階段：{self.phase}"]
        if self.lobby_players:
            lines.append("大廳玩家：" + ", ".join(
                p.get("id", str(p)) if isinstance(p, dict) else str(p)
                for p in self.lobby_players
            ))
        if self.order:
            lines.append("輪轉順序：" + " → ".join(self.order))
        if self.turn_id is not None:
            lines.append(f"目前回合：{self.turn_id}，喊數者：{self.guesser}")
        if self.current_guess is not None:
            lines.append(f"目前喊數：{self.current_guess}；回報者：{', '.join(self.responders)}")
        if self.eliminated:
            lines.append("淘汰：" + ", ".join(self.eliminated))
        if self.forfeited:
            lines.append("棄權：" + ", ".join(self.forfeited))
        if self.disqualified:
            lines.append("取消資格：" + ", ".join(self.disqualified))
        if self.reveals:
            lines.append("揭示判定：" + "; ".join(
                f"{r.get('player')}={r.get('verdict')}" for r in self.reveals
            ))
        if self.ranking:
            lines.append("最終名次：" + "; ".join(
                f"第 {r.get('rank')} 名 {r.get('player')} ({r.get('status')})"
                for r in self.ranking
            ))
        return "\n".join(lines)



# ---------------- C12 & C13 整合內建驗收測試 ----------------
def _run_all_session_and_display_tests():
    import hashlib

    print("🚀 開始執行 ClientSession (C12) 與 ClientDisplay (C13) 驗收測試...\n")

    # =========================================================
    # 第一部分：驗證 C12 (ClientSession)
    # =========================================================
    print("[Part 1/2] 驗證 ClientSession（秘密防呆、預填回報、修改二次確認）...")
    sess = ClientSession()

    # 1. 擋掉非法秘密
    for bad in ("1123", "123", "abcd"):
        try:
            sess.set_secret(bad)
            assert False, f"應攔截非法秘密 {bad}"
        except ValueError:
            pass

    # 2. 設定合法秘密並驗證 COMMIT & REVEAL
    c_msg = sess.set_secret("3851")
    r_msg = sess.reveal_message()
    exp_hash = hashlib.sha256(f"3851:{sess.salt}".encode("utf-8")).hexdigest()
    assert c_msg == {"type": "COMMIT", "commit": exp_hash}
    assert r_msg == {"type": "REVEAL", "secret": "3851", "salt": sess.salt}

    # 3. 預填回報與修改二次確認
    draft = sess.prepare_feedback(turn_id=1, guess="3890")  # 3851 vs 3890 -> 2A0B
    assert draft == {"type": "FEEDBACK", "turn_id": 1, "a": 2, "b": 0}

    # 修改數值但未二次確認 -> 必須失敗
    try:
        sess.submit_feedback(turn_id=1, a=1, b=1, confirmed=False)
        assert False, "修改自動計算結果未二次確認應被阻擋"
    except ValueError:
        pass

    # 非法組合 3A1B -> 必須失敗
    try:
        sess.submit_feedback(turn_id=1, a=3, b=1, confirmed=True)
        assert False, "非法組合 3A1B 應被阻擋"
    except ValueError:
        pass

    # 二次確認後放行
    final_fb = sess.submit_feedback(turn_id=1, a=1, b=1, confirmed=True)
    assert final_fb == {"type": "FEEDBACK", "turn_id": 1, "a": 1, "b": 1}
    print("  ✅ C12 全部通過！\n")

    # =========================================================
    # 第二部分：驗證 C13 (ClientDisplay 六大資訊呈現)
    # =========================================================
    print("[Part 2/2] 驗證 ClientDisplay（大廳、輪轉、全部回報、淘汰、判定、名次）...")
    disp = ClientDisplay()

    # 1. 大廳 (LOBBY)
    disp.handle({
        "type": "LOBBY",
        "players": [
            {"id": "p1", "player_id": "p1", "name": "G09-Human"},
            {"id": "p2", "player_id": "p2", "name": "AlphaBot"},
            {"id": "p3", "player_id": "p3", "name": "BetaBot"},
        ],
        "min_players": 2,
        "max_players": 3,
    })
    out_lobby = disp.render()
    assert "LOBBY" in out_lobby and "p1" in out_lobby and "p2" in out_lobby, "大廳資訊缺少玩家清單"

    # 2. 輪轉順序 (GAME_START)
    disp.handle({
        "type": "GAME_START",
        "game_id": "g-test-13",
        "players": ["p2", "p1", "p3"],
        "order": ["p2", "p1", "p3"],
        "rules": {},
    })
    out_start = disp.render()
    assert "p2 → p1 → p3" in out_start, "未正確呈現輪轉順序"

    # 3. 喊數與全部回報 + 淘汰 (TURN -> GUESS_MADE -> TURN_RESULT)
    disp.handle({"type": "TURN", "turn_id": 1, "guesser": "p2", "alive": ["p2", "p1", "p3"]})
    disp.handle({"type": "GUESS_MADE", "turn_id": 1, "guesser": "p2", "guess": "8642", "responders": ["p1", "p3"]})
    disp.handle({
        "type": "TURN_RESULT",
        "turn_id": 1,
        "guesser": "p2",
        "guess": "8642",
        "skipped": False,
        "results": [
            {"player_id": "p1", "a": 0, "b": 1},
            {"player_id": "p3", "a": 4, "b": 0},
        ],
        "eliminated": ["p3"],
        "forfeited": [],
        "disqualified": [],
    })
    out_turn = disp.render()
    assert "p1 0A1B" in disp.events[-1] and "p3 4A0B" in disp.events[-1], "未記錄該回合全部回報"
    assert "淘汰：p3" in out_turn, "未顯示被淘汰玩家"

    # 4. 揭示判定 (REVEALED)
    disp.handle({"type": "REVEALED", "player": "p1", "secret": "3851", "salt": "a" * 32, "verdict": "OK", "bad_turns": []})
    disp.handle({"type": "REVEALED", "player": "p2", "secret": "9753", "salt": "b" * 32, "verdict": "INCONSISTENT", "bad_turns": [2]})
    out_rev = disp.render()
    assert "p1=OK" in out_rev and "p2=INCONSISTENT" in out_rev, "未正確顯示揭示判定結果"

    # 5. 最終名次 (GAME_OVER)
    disp.handle({
        "type": "GAME_OVER",
        "game_id": "g-test-13",
        "reason": "LAST_PLAYER",
        "turns": 1,
        "ranking": [
            {"rank": 1, "player_id": "p1", "player": "p1", "status": "SURVIVOR", "out_turn": None},
            {"rank": 2, "player_id": "p3", "player": "p3", "status": "GUESSED", "out_turn": 1},
            {"rank": None, "player_id": "p2", "player": "p2", "status": "DQ", "out_turn": 1},
        ],
    })
    out_over = disp.render()
    assert "SURVIVOR" in out_over and "GUESSED" in out_over and "DQ" in out_over, "最終名次缺少狀態顯示"

    print("\n--- 模擬最終畫面輸出預覽 ---")
    print(out_over)
    print("----------------------------")
    print("\n🎉 恭喜！C12 與 C13（六大資訊呈現）全部驗收通過！")


if __name__ == "__main__":
    _run_all_session_and_display_tests()