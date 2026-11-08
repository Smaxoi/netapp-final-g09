"""A4/A5：BC/1.0 訊息建構與欄位檢查（PDF §9、§11、§13）。

建構器回傳 dict，交給 wire.encode 發送；錯誤輸入拋出 ProtocolError。
check_client_fields 只檢查必填欄位與 JSON 型別。A7 必須先呼叫它，再依
§13 檢查階段、回合、身分、重複，最後才呼叫 check_client_values。
validate_client_message 適合本地建構，不代替伺服器的狀態檢查。

validate_server_message 檢查全部 14 種伺服器訊息。未知欄位一律忽略；
未知伺服器訊息回 None，呼叫端仍須依 SERVER_TYPES 決定是否分派。
輪轉順序、目前回合與歷史回報等跨訊息條件留給遊戲狀態處理器。
"""

from dataclasses import dataclass
from typing import Callable

from .errors import (
    BAD_FIELD, BAD_JSON, ERROR_CODES, INVALID_VALUE, UNKNOWN_TYPE,
    VERSION_UNSUPPORTED, ProtocolError,
)
from .validators import (
    is_int, valid_feedback, valid_game_id, valid_hex32, valid_hex64,
    valid_number, valid_player_id, valid_text, valid_timeout, valid_version,
)


@dataclass(frozen=True)
class _Field:
    """分開保存型別與值條件，確保所有型別錯誤先於值錯誤。"""

    check_type: Callable[[object], bool]
    check_value: Callable[[object], bool]


def _object(required, optional=None):
    optional = optional or {}

    def check_type(value):
        return (isinstance(value, dict)
                and all(key in value and field.check_type(value[key])
                        for key, field in required.items())
                and all(key not in value or field.check_type(value[key])
                        for key, field in optional.items()))

    def check_value(value):
        return (all(field.check_value(value[key]) for key, field in required.items())
                and all(key not in value or field.check_value(value[key])
                        for key, field in optional.items()))

    return _Field(check_type, check_value)


def _array(item, unique_key=None):
    def check_value(value):
        if not all(item.check_value(entry) for entry in value):
            return False
        return (unique_key is None
                or len({unique_key(entry) for entry in value}) == len(value))

    return _Field(
        lambda value: isinstance(value, list) and all(item.check_type(entry) for entry in value),
        check_value,
    )


def _nullable(field):
    return _Field(
        lambda value: value is None or field.check_type(value),
        lambda value: value is None or field.check_value(value),
    )


def _string(validator=lambda value: True):
    return _Field(lambda value: isinstance(value, str), validator)


_STRING = _string()
_INT = _Field(is_int, lambda value: True)
_POS_INT = _Field(is_int, lambda value: value >= 1)
_NONNEG_INT = _Field(is_int, lambda value: value >= 0)
_AB = _Field(is_int, lambda value: 0 <= value <= 4)
_BOOL = _Field(lambda value: type(value) is bool, lambda value: True)
_TIMEOUT = _Field(lambda value: type(value) in (int, float), valid_timeout)
_NAME = _string(valid_text)
_KIND = _string(lambda value: value in {"human", "ai"})
_PROTOCOL = _string(valid_version)
_PID = _string(valid_player_id)
_GAME_ID = _string(valid_game_id)
_NUMBER = _string(valid_number)
_HEX64 = _string(valid_hex64)
_HEX32 = _string(valid_hex32)
_TEXT = _string(lambda value: valid_text(value, 1, 200))
_PIDS = _array(_PID, unique_key=lambda value: value)
_PLAYER = _object({"id": _PID, "name": _NAME, "team": _NAME, "kind": _KIND})
_PLAYERS = _array(_PLAYER, unique_key=lambda player: player["id"])
_RULES = _object({
    "guess_timeout": _TIMEOUT, "feedback_timeout": _TIMEOUT,
    "commit_timeout": _TIMEOUT, "reveal_timeout": _TIMEOUT,
    "max_turns": _POS_INT, "max_skips": _POS_INT,
})
_COMMITS = _Field(
    lambda value: isinstance(value, dict)
    and all(isinstance(key, str) and isinstance(commit, str) for key, commit in value.items()),
    lambda value: all(valid_player_id(key) and valid_hex64(commit) for key, commit in value.items()),
)
_RESULT = _object({"player": _PID, "a": _AB, "b": _AB})
_RESULTS = _array(_RESULT, unique_key=lambda result: result["player"])
_RANK = _object({
    "player": _PID,
    "status": _string(lambda value: value in {"SURVIVOR", "GUESSED", "FORFEIT", "DQ"}),
    "out_turn": _nullable(_POS_INT),
    "rank": _nullable(_POS_INT),
})

_CLIENT_SCHEMAS = {
    "HELLO": _object({"protocol": _PROTOCOL, "name": _NAME, "team": _NAME, "kind": _KIND},
                     {"software": _STRING}),
    "COMMIT": _object({"commit": _HEX64}),
    "GUESS": _object({"turn_id": _POS_INT, "guess": _NUMBER}),
    "FEEDBACK": _object({"turn_id": _POS_INT, "a": _AB, "b": _AB}),
    "REVEAL": _object({"secret": _NUMBER, "salt": _HEX32}),
    "CHAT": _object({"text": _TEXT}),
    "PING": _object({}, {"nonce": _STRING}),
    "PONG": _object({}, {"nonce": _STRING}),
    "BYE": _object({}),
}

_SERVER_SCHEMAS = {
    "WELCOME": _object({"protocol": _PROTOCOL, "player_id": _PID}, {"server": _STRING}),
    "LOBBY": _object({"players": _PLAYERS, "min_players": _POS_INT, "max_players": _POS_INT}),
    "GAME_START": _object({"game_id": _GAME_ID, "players": _PLAYERS, "order": _PIDS, "rules": _RULES}),
    "COMMITS": _object({"order": _PIDS, "commits": _COMMITS}),
    "TURN": _object({"turn_id": _POS_INT, "guesser": _PID, "alive": _PIDS}),
    "GUESS_MADE": _object({"turn_id": _POS_INT, "guesser": _PID, "guess": _NUMBER, "responders": _PIDS}),
    "TURN_RESULT": _object({
        "turn_id": _POS_INT, "guesser": _PID, "guess": _nullable(_NUMBER),
        "skipped": _BOOL, "results": _RESULTS,
        "eliminated": _PIDS, "forfeited": _PIDS, "disqualified": _PIDS,
    }),
    "REVEAL_REQUEST": _object({"reason": _string(lambda value: value in {"ELIMINATED", "GAME_END"})}),
    "REVEALED": _object({
        "player": _PID, "secret": _nullable(_NUMBER), "salt": _nullable(_HEX32),
        "verdict": _string(lambda value: value in {"OK", "BAD_COMMIT", "INCONSISTENT", "NO_REVEAL"}),
        "bad_turns": _array(_POS_INT, unique_key=lambda value: value),
    }),
    "GAME_OVER": _object({
        "game_id": _GAME_ID,
        "reason": _string(lambda value: value in {"LAST_PLAYER", "MAX_TURNS", "ABORTED"}),
        "turns": _NONNEG_INT, "ranking": _array(_RANK, unique_key=lambda row: row["player"]),
    }),
    "CHAT": _object({"from": _PID, "text": _TEXT}),
    "PING": _object({}, {"nonce": _STRING}),
    "PONG": _object({}, {"nonce": _STRING}),
    "ERROR": _object({"code": _string(lambda value: value in ERROR_CODES),
                       "message": _STRING, "fatal": _BOOL}),
}

CLIENT_TYPES = frozenset(_CLIENT_SCHEMAS)
SERVER_TYPES = frozenset(_SERVER_SCHEMAS)


def _check_fields(msg, schemas, *, ignore_unknown=False):
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return BAD_JSON
    schema = schemas.get(msg["type"])
    if schema is None:
        return None if ignore_unknown else UNKNOWN_TYPE
    return None if schema.check_type(msg) else BAD_FIELD


def check_client_fields(msg: dict) -> str | None:
    """僅檢查欄位存在及 JSON 型別；不提前判斷值、版本或回合是否合法。"""
    return _check_fields(msg, _CLIENT_SCHEMAS)


def check_client_values(msg: dict) -> str | None:
    """欄位格式檢查；伺服器須在 §13 指定的值檢查位置才呼叫。"""
    error = check_client_fields(msg)
    if error:
        return error
    if msg["type"] == "HELLO" and not valid_version(msg["protocol"]):
        return VERSION_UNSUPPORTED
    if not _CLIENT_SCHEMAS[msg["type"]].check_value(msg):
        return INVALID_VALUE
    if msg["type"] == "FEEDBACK" and not valid_feedback(msg["a"], msg["b"]):
        return INVALID_VALUE
    return None


def validate_client_message(msg: dict) -> str | None:
    """本地完整欄位檢查；不包含 A7 的階段、身分、重複或時限判斷。"""
    return check_client_values(msg)


def _server_semantics(msg):
    kind = msg["type"]
    if kind in {"LOBBY", "GAME_START"}:
        names = [player["name"] for player in msg["players"]]
        if len(set(names)) != len(names):
            return False
    if kind == "LOBBY":
        return msg["min_players"] == 2 and msg["max_players"] >= 2 and len(msg["players"]) <= msg["max_players"]
    if kind == "GAME_START":
        return len(msg["order"]) >= 2 and msg["order"] == [player["id"] for player in msg["players"]]
    if kind == "COMMITS":
        return len(msg["order"]) >= 2 and set(msg["order"]) == set(msg["commits"])
    if kind == "TURN":
        return msg["guesser"] in msg["alive"]
    if kind == "GUESS_MADE":
        return msg["guesser"] not in msg["responders"]
    if kind == "TURN_RESULT":
        if msg["skipped"] != (msg["guess"] is None):
            return False
        if msg["skipped"] and msg["results"]:
            return False
        if any(row["player"] == msg["guesser"] or not valid_feedback(row["a"], row["b"])
               for row in msg["results"]):
            return False
        departures = msg["eliminated"] + msg["forfeited"] + msg["disqualified"]
        return len(set(departures)) == len(departures)
    if kind == "REVEALED":
        missing = msg["verdict"] == "NO_REVEAL"
        if missing != (msg["secret"] is None) or missing != (msg["salt"] is None):
            return False
        return bool(msg["bad_turns"]) == (msg["verdict"] == "INCONSISTENT")
    if kind == "GAME_OVER":
        if msg["reason"] == "ABORTED":
            return msg["turns"] == 0 and msg["ranking"] == []
        if msg["turns"] < 1 or not msg["ranking"]:
            return False
        previous_rank = 0
        seen_dq = False
        for row in msg["ranking"]:
            status, rank, turn = row["status"], row["rank"], row["out_turn"]
            if turn is not None and not 1 <= turn <= msg["turns"]:
                return False
            if (status == "SURVIVOR") != (turn is None):
                return False
            if status == "DQ":
                if rank is not None:
                    return False
                seen_dq = True
            else:
                if rank is None or seen_dq or rank < previous_rank:
                    return False
                previous_rank = rank
        return True
    return True


def validate_server_message(msg: dict) -> str | None:
    """檢查 14 種伺服器訊息；未知類型依 §8.3 忽略，回 None。"""
    error = _check_fields(msg, _SERVER_SCHEMAS, ignore_unknown=True)
    if error:
        return error
    if msg["type"] not in SERVER_TYPES:
        return None
    if msg["type"] == "WELCOME" and not valid_version(msg["protocol"]):
        return VERSION_UNSUPPORTED
    if not _SERVER_SCHEMAS[msg["type"]].check_value(msg) or not _server_semantics(msg):
        return INVALID_VALUE
    return None


def build_client_message(message_type: str, **fields) -> dict:
    """通用客戶端建構器；支援全部 9 種訊息，保留擴充欄位。"""
    msg = {"type": message_type, **fields}
    error = validate_client_message(msg)
    if error:
        raise ProtocolError(error)
    return msg


def hello(name: str, team: str, kind: str, *, protocol: str = "BC/1.0",
          software: str | None = None) -> dict:
    fields = {"protocol": protocol, "name": name, "team": team, "kind": kind}
    if software is not None:
        fields["software"] = software
    return build_client_message("HELLO", **fields)


def commit(value: str) -> dict:
    return build_client_message("COMMIT", commit=value)


def guess(turn_id: int, value: str) -> dict:
    return build_client_message("GUESS", turn_id=turn_id, guess=value)


def feedback(turn_id: int, a: int, b: int) -> dict:
    return build_client_message("FEEDBACK", turn_id=turn_id, a=a, b=b)


def reveal(secret: str, salt: str) -> dict:
    return build_client_message("REVEAL", secret=secret, salt=salt)


_MISSING = object()


def pong(nonce=_MISSING) -> dict:
    """無 nonce 時省略欄位；有 nonce 時原樣保留，包含空字串。"""
    return build_client_message("PONG", **({} if nonce is _MISSING else {"nonce": nonce}))
