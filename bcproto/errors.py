"""A6：PDF §13 的 15 種錯誤碼、致命條件與 ERROR 訊息。

本模組不讀遊戲狀態、不計算錯誤次數；檢查順序與第 21 次錯誤的送出流程
分別由 A7 與 A13 負責。hello_done 指這條連線是否已完成 HELLO。
"""

from types import MappingProxyType

BAD_JSON = "BAD_JSON"
LINE_TOO_LONG = "LINE_TOO_LONG"
UNKNOWN_TYPE = "UNKNOWN_TYPE"
BAD_FIELD = "BAD_FIELD"
INVALID_VALUE = "INVALID_VALUE"
UNEXPECTED = "UNEXPECTED"
NOT_YOUR_TURN = "NOT_YOUR_TURN"
STALE_TURN = "STALE_TURN"
DUPLICATE = "DUPLICATE"
VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
GAME_IN_PROGRESS = "GAME_IN_PROGRESS"
ROOM_FULL = "ROOM_FULL"
NAME_TAKEN = "NAME_TAKEN"
TIMEOUT = "TIMEOUT"
TOO_MANY_ERRORS = "TOO_MANY_ERRORS"

ERROR_MESSAGES = MappingProxyType({
    BAD_JSON: "Message must be a JSON object with a string type.",
    LINE_TOO_LONG: "Message exceeds 4096 bytes including LF.",
    UNKNOWN_TYPE: "Unknown message type.",
    BAD_FIELD: "A required field is missing or has the wrong JSON type.",
    INVALID_VALUE: "A field value is invalid.",
    UNEXPECTED: "Message is not allowed in the current phase or role.",
    NOT_YOUR_TURN: "It is not your turn to guess.",
    STALE_TURN: "turn_id does not match the current turn.",
    DUPLICATE: "Message has already been accepted.",
    VERSION_UNSUPPORTED: "Protocol must match BC/1.[0-9]+.",
    GAME_IN_PROGRESS: "A game is already in progress.",
    ROOM_FULL: "The lobby is full.",
    NAME_TAKEN: "The player name is already in use.",
    TIMEOUT: "HELLO or COMMIT deadline expired.",
    TOO_MANY_ERRORS: "More than 20 errors on this connection.",
})
ERROR_CODES = frozenset(ERROR_MESSAGES)
FATAL_CODES = frozenset({
    LINE_TOO_LONG, VERSION_UNSUPPORTED, GAME_IN_PROGRESS, ROOM_FULL,
    NAME_TAKEN, TIMEOUT, TOO_MANY_ERRORS,
})


class ProtocolError(ValueError):
    """本地訊息建構失敗；code 為對應協定錯誤碼。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_fatal(code: str, *, message_type: str | None = None,
             hello_done: bool = False) -> bool:
    """依 §13 判斷 fatal；重送 HELLO 由 dispatch 回 UNEXPECTED。

    BAD_FIELD/INVALID_VALUE 只在首次 HELLO 時致命；UNEXPECTED 只在完成
    HELLO 之前致命。BAD_JSON 即使在握手前也不是致命錯誤。
    """
    if code not in ERROR_CODES:
        raise ValueError(f"Unknown error code: {code}")
    if code in FATAL_CODES:
        return True
    if code in {BAD_FIELD, INVALID_VALUE}:
        return message_type == "HELLO" and not hello_done
    if code == UNEXPECTED:
        return not hello_done
    return False


def error_message(code: str, message: str | None = None, *,
                  message_type: str | None = None,
                  hello_done: bool = False) -> dict:
    """建構 ERROR dict；交給 wire.encode 與連線層發送。"""
    fatal = is_fatal(code, message_type=message_type, hello_done=hello_done)
    if message is None:
        message = ERROR_MESSAGES[code]
    if not isinstance(message, str):
        raise TypeError("message must be a string")
    return {"type": "ERROR", "code": code, "message": message, "fatal": fatal}
