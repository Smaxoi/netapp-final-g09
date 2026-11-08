
"""PDF §9 的純欄位驗證器；型別與值檢查由 messages 分兩階段套用。"""

import math
import re
import unicodedata

from .rules import VALID_FEEDBACK


def valid_number(value):
    # 必須是四位 ASCII 數字，且不能重複
    return (
        isinstance(value, str)
        and len(value) == 4
        and all("0" <= ch <= "9" for ch in value)
        and len(set(value)) == 4
    )


def is_int(value):
    # Python 中 bool 是 int 的子類別
    # 所以不能直接用 isinstance(value, int)
    return type(value) is int


def valid_text(value, min_len=1, max_len=20):
    # 必須是字串，長度在指定範圍內
    if not isinstance(value, str):
        return False

    if not min_len <= len(value) <= max_len:
        return False

    # Unicode 類別 C：控制類字元
    return all(
        not unicodedata.category(ch).startswith("C")
        for ch in value
    )


def valid_hex64(value):
    # SHA-256 commit：64 個小寫 hex 字元
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def valid_hex32(value):
    # Salt：32 個小寫 hex 字元
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{32}", value) is not None
    )


def valid_version(value):
    # BC/1.0、BC/1.1 等符合版本格式
    return (
        isinstance(value, str)
        and re.fullmatch(r"BC/1\.[0-9]+", value) is not None
    )


def valid_player_id(value):
    return isinstance(value, str) and re.fullmatch(r"p[1-9][0-9]*", value) is not None


def valid_game_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def valid_feedback(a, b):
    return is_int(a) and is_int(b) and (a, b) in VALID_FEEDBACK


def valid_timeout(value):
    # bool 不是 JSON number；先區分 int，避免巨大整數轉 float 時溢位。
    return ((is_int(value) and value > 0)
            or (type(value) is float and math.isfinite(value) and value > 0))
