"""A7：PDF §12.1、§13 的訊息檢查順序。

check_message 只讀 Server狀態契約.md 的欄位，不發送訊息、不更動遊戲狀態。
回傳第一個錯誤碼；None 表示通過，供 A10/A11 處理器填答並 changed.set()。
player=None 表示尚未完成 HELLO。A10 必須在 GAME_OVER 後直接忽略訊息，
並在檢查與填答之間避免 await，以免檢查結果與實際狀態不同步。
"""

from collections.abc import Collection, Mapping
from typing import Protocol

from ..errors import (
    BAD_JSON, DUPLICATE, GAME_IN_PROGRESS, NAME_TAKEN, NOT_YOUR_TURN,
    ROOM_FULL, STALE_TURN, UNEXPECTED,
)
from ..messages import check_client_fields, check_client_values


class PlayerState(Protocol):
    """A7 會讀取的 Player 欄位，不要求繼承此類別。"""

    pid: str
    name: str
    status: str
    commit: str | None
    reveal: tuple[str, str] | None


class _Options(Protocol):
    players: int


class ServerState(Protocol):
    """對應 Server 狀態契約；B8 的真實 Server 可直接傳入。"""

    a: _Options
    phase: str
    players: Mapping[str, PlayerState]
    turn_id: int
    guesser: str | None
    guess_open: bool
    fb_open: bool
    responders: Collection[str]
    feedback: Mapping[str, tuple[int, int]]
    reveal_wait: Collection[str]


def check_message(msg: dict, server: ServerState,
                  player: PlayerState | None = None) -> str | None:
    """按規範順序回傳錯誤碼；不以 INVALID_VALUE 蓋過階段或回合錯誤。

    HELLO 完成前，非物件／缺少字串 type 回 BAD_JSON；其他非 HELLO
    訊息（包括未知類型）一律回 UNEXPECTED。完成後重送 HELLO 也回
    UNEXPECTED，其 fatal 由 A6 依握手狀態決定。
    """
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return BAD_JSON
    kind = msg["type"]
    if player is None:
        if kind != "HELLO":
            return UNEXPECTED
    elif kind == "HELLO":
        return UNEXPECTED

    error = check_client_fields(msg)
    if error:
        return error

    if kind == "HELLO":
        # 欄位 → 版本 → 值 → 階段 → 人數 → 名稱。
        error = check_client_values(msg)
        if error:
            return error
        if server.phase != "LOBBY":
            return GAME_IN_PROGRESS
        if len(server.players) >= server.a.players:
            return ROOM_FULL
        if any(existing.name == msg["name"] for existing in server.players.values()):
            return NAME_TAKEN
        return None

    if kind == "COMMIT":
        if (server.phase != "COMMIT"
                or server.players.get(player.pid) is not player
                or player.status != "ALIVE"):
            return UNEXPECTED
        if player.commit is not None:
            return DUPLICATE

    elif kind == "GUESS":
        if server.phase != "PLAY":
            return UNEXPECTED
        if msg["turn_id"] != server.turn_id:
            return STALE_TURN
        if player.pid != server.guesser:
            return NOT_YOUR_TURN
        if not server.guess_open:
            return UNEXPECTED

    elif kind == "FEEDBACK":
        if server.phase != "PLAY":
            return UNEXPECTED
        if msg["turn_id"] != server.turn_id:
            return STALE_TURN
        if not server.fb_open or player.pid not in server.responders:
            return UNEXPECTED
        if player.pid in server.feedback:
            return DUPLICATE

    elif kind == "REVEAL":
        if player.reveal is not None:
            return DUPLICATE
        if player.pid not in server.reveal_wait:
            return UNEXPECTED

    # CHAT: BAD_FIELD → INVALID_VALUE。
    # PING/PONG 的選填 nonce 亦須為字串；BYE 無額外欄位。
    # REVEAL 只驗格式，承諾值與回報是否誠實由 B14 的揭示驗證判定。
    return check_client_values(msg)
