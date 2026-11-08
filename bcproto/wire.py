
"""BC/1.0 JSON Lines 編解碼；接收切行與長度限制由 A9 連線層負責。"""

import json
import re

MAX_LINE = 4096

def encode(msg: dict) -> bytes:
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError("BAD_JSON")
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", msg["type"]) is None:
        raise ValueError("INVALID_VALUE")

    # json.dumps 會把整數鍵轉成字串，可能使 1 與 "1" 變成重複鍵。
    # 所有層級都只接受字串鍵；已走訪容器不重複展開，循環由 dumps 拒絕。
    pending = [msg]
    seen = set()
    while pending:
        value = pending.pop()
        if isinstance(value, (dict, list, tuple)):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("BAD_FIELD")
                pending.extend(value.values())
            else:
                pending.extend(value)

    # 轉成 JSON 字串，禁止 NaN / Infinity
    text = json.dumps(
        msg,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False
    )

    # 使用 UTF-8 編碼，最後加上換行
    data = (text + "\n").encode("utf-8")

    # BC/1.0 每則訊息最多 4096 bytes
    if len(data) > MAX_LINE:
        raise ValueError("LINE_TOO_LONG")

    return data


def reject_constant(value):
    # JSON 不允許 NaN、Infinity、-Infinity 這行raise會自己知道這三個不可
    raise ValueError("Invalid JSON constant")


def reject_duplicate(pairs):
    # JSON 物件不能有重複的 key
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value

    return result


def decode_line(raw: bytes) -> tuple[dict | None, str | None]:
    """解析已由連線層切出的單行；空行回 (None, None)，壞資料回 BAD_JSON。"""
    try:
        text = raw.decode("utf-8")

        # 移除最後的 LF，並容許 LF 前有 CR
        if text.endswith("\n"):
            text = text[:-1]

        if text.endswith("\r"):
            text = text[:-1]

        # 空行直接忽略
        if text == "":
            return None, None

        # JSON 字串 -> Python 物件
        msg = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate
        )

        # 頂層必須是物件，且 type 必須為字串
        if not isinstance(msg, dict):
            return None, "BAD_JSON"

        if not isinstance(msg.get("type"), str):
            return None, "BAD_JSON"

        return msg, None

    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "BAD_JSON"
