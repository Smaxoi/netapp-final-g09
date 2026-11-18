"""人類玩家的協定狀態與 C12 回報流程。"""

from __future__ import annotations

import secrets
import json
import os
import time
import uuid

from ..messages import commit, feedback, reveal
from ..rules import make_commit, score, verify_reveal
from ..validators import valid_feedback, valid_number


class ClientJSONLLogger:
    """C16：寫入 client 專用目錄的 JSON Lines 日誌。"""

    def __init__(self, log_dir: str = "client-logs", peer: str = "server"):
        self.log_dir = os.fspath(log_dir)
        if os.path.abspath(self.log_dir) == os.path.abspath("logs"):
            raise ValueError("客戶端日誌不得寫入 server 的 logs 目錄")
        self.peer = peer
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(self.log_dir, f"{stamp}-{uuid.uuid4().hex[:12]}.jsonl")
        self.file = open(self.path, "w", encoding="utf-8")

    def log(self, direction: str, msg: dict, peer: str | None = None) -> None:
        if direction not in {"send", "recv"}:
            raise ValueError("direction 必須是 send 或 recv")
        record = {"ts": round(time.time(), 3), "dir": direction,
                  "peer": peer or self.peer, "msg": msg}
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()

    def log_raw(self, direction: str, text: str, peer: str | None = None) -> None:
        self.log(direction, {"raw": text}, peer)

    def close(self) -> None:
        if not self.file.closed:
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ClientSession:
    """保存本局秘密，並處理預填回報與修改確認。"""

    def __init__(self):
        self.secret: str | None = None
        self.salt: str | None = None
        self.commit: str | None = None
        self.player_id: str | None = None
        self.records: list[tuple[int, str, int, int]] = []
        self._feedback_drafts: dict[int, tuple[str, int, int]] = {}

    def handle_welcome(self, msg: dict) -> None:
        """記住 server 分配的 player_id，供本地揭示驗證使用。"""
        self.player_id = msg.get("player_id")

    def set_secret(self, secret: str) -> dict:
        """設定秘密並建立 COMMIT；秘密必須是四位不重複 ASCII 數字。"""
        if not valid_number(secret):
            raise ValueError("秘密必須是四位不重複 ASCII 數字")
        self.secret = secret
        self.salt = secrets.token_hex(16)
        self.commit = make_commit(secret, self.salt)
        self.records.clear()
        self._feedback_drafts.clear()
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
        self.records.append((turn_id, draft[0], actual_a, actual_b))
        return feedback(turn_id, actual_a, actual_b)

    def verify_revealed(self, msg: dict) -> dict | None:
        """C14：本地重算 REVEALED，不盲信 server 的 verdict。

        非自己玩家的揭示回傳 None；自己的揭示回傳本地判定、server 判定
        以及兩者是否一致。
        """
        revealed_player = msg.get("player_id", msg.get("player"))
        if self.player_id is not None and revealed_player != self.player_id:
            return None
        if self.commit is None:
            raise ValueError("尚未建立 COMMIT，無法驗證揭示")

        secret = msg.get("secret")
        salt = msg.get("salt")
        local_verdict, local_bad_turns = verify_reveal(
            self.commit, self.records, secret, salt
        )
        server_verdict = msg.get("verdict")
        server_bad_turns = msg.get("bad_turns", [])
        return {
            "player": revealed_player,
            "local_verdict": local_verdict,
            "local_bad_turns": local_bad_turns,
            "server_verdict": server_verdict,
            "server_bad_turns": server_bad_turns,
            "consistent": (
                local_verdict == server_verdict
                and local_bad_turns == server_bad_turns
            ),
        }


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
        self.closed = False
        self.close_reason: str | None = None

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
        elif kind == "ERROR":
            code = msg.get("code", "UNKNOWN")
            message = msg.get("message", "伺服器回報錯誤")
            self.events.append(f"伺服器錯誤 [{code}]：{message}")
            if msg.get("fatal"):
                self.closed = True
                self.close_reason = f"伺服器致命錯誤 [{code}]：{message}"
                self.phase = "CLOSED"

    def connection_lost(self, error: BaseException | None = None) -> None:
        """C15：連線中斷時保存清楚的結束原因。"""
        self.closed = True
        self.phase = "CLOSED"
        detail = str(error).strip() if error else "伺服器已關閉連線"
        self.close_reason = f"連線已中斷：{detail}"
        self.events.append(self.close_reason)

    @staticmethod
    def _format_results(results: list[dict]) -> str:
        return ", ".join(
            f"{row.get('player', row.get('player_id'))} {row.get('a')}A{row.get('b')}B"
            for row in results
        ) or "無回報"

    def render(self) -> str:
        """回傳可直接印到終端機的完整資訊摘要。"""
        lines = [f"階段：{self.phase}"]
        if self.close_reason:
            lines.append(self.close_reason)
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



