"""§8.3 與 A1/A2 的框架及異常輸入測試（不需要第三方套件）。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.wire import MAX_JSON_DEPTH, MAX_LINE, decode_line, encode


class WireTests(unittest.TestCase):
    def test_utf8_round_trip_and_escaped_newlines(self):
        msg = {"type": "CHAT", "text": "中文\n第二行\r\n"}
        raw = encode(msg)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn("中文".encode("utf-8"), raw)
        self.assertEqual(decode_line(raw), (msg, None))

    def test_exact_byte_limit_includes_lf(self):
        overhead = len(encode({"type": "CHAT", "text": ""}))
        self.assertEqual(len(encode({"type": "CHAT", "text": "x" * (MAX_LINE - overhead)})), MAX_LINE)
        for text in ["x" * (MAX_LINE - overhead + 1), "中" * 1400]:
            with self.subTest(text_length=len(text)), self.assertRaisesRegex(ValueError, "LINE_TOO_LONG"):
                encode({"type": "CHAT", "text": text})

    def test_nonfinite_numbers_cannot_be_sent(self):
        for value in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode({"type": "PING", "extra": value})

    def test_encoder_rejects_nonstring_keys_at_all_depths(self):
        for msg in [
            {"type": "PING", 1: "a", "1": "b"},
            {"type": "PING", "extra": [{1: "a", "1": "b"}]},
        ]:
            with self.subTest(msg=msg), self.assertRaisesRegex(ValueError, "BAD_FIELD"):
                encode(msg)

    def test_encoder_requires_object_and_uppercase_type(self):
        for msg in [[], {}, {"type": True}, {"type": "ping"}, {"type": ""}]:
            with self.subTest(msg=msg), self.assertRaises(ValueError):
                encode(msg)

    def test_shared_objects_allowed_but_cycles_rejected(self):
        shared = {"key": 1}
        msg = {"type": "PING", "extra": [shared, shared]}
        self.assertEqual(json.loads(encode(msg)), msg)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(ValueError):
            encode({"type": "PING", "extra": cyclic})

    def test_crlf_and_empty_lines(self):
        self.assertEqual(decode_line(b'{"type":"PING"}\r\n'), ({"type": "PING"}, None))
        for raw in [b"", b"\n", b"\r\n"]:
            with self.subTest(raw=raw):
                self.assertEqual(decode_line(raw), (None, None))

    def test_bad_json_returns_error(self):
        for raw in [
            b"\xff\n", b'\xef\xbb\xbf{"type":"PING"}\n',
            b"[]\n", b"null\n", b"42\n", b'"text"\n', b"{}\n",
            b'{"type":null}\n', b'{"type":1}\n', b'{"type":true}\n',
            b'{"type":"PING",}\n', b'{"type":"CHAT","text":"a\nb"}\n',
            b'{"type":"PING"}{"type":"PONG"}\n',
            b'{"type":"PING","n":NaN}\n', b'{"type":"PING","n":Infinity}\n',
            b'{"type":"PING","n":-Infinity}\n',
            b'{"type":"PING","type":"PONG"}\n',
            b'{"type":"PING","extra":{"key":1,"key":2}}\n',
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(decode_line(raw), (None, "BAD_JSON"))

    def test_deep_json_cannot_escape_as_recursion_error(self):
        raw = b'{"type":"PING","extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n"
        self.assertLessEqual(len(raw), MAX_LINE)
        self.assertEqual(decode_line(raw), (None, "BAD_JSON"))
        malformed = b'{"type":"PING","extra":' + b"[" * 4000 + b"\n"
        self.assertLessEqual(len(malformed), MAX_LINE)
        self.assertEqual(decode_line(malformed), (None, "BAD_JSON"))

    def test_depth_limit_includes_outer_object_and_accepts_exact_boundary(self):
        # 外層物件為第一層，其內再放 MAX_JSON_DEPTH - 1 層陣列。
        levels = MAX_JSON_DEPTH - 1
        raw = b'{"type":"PING","extra":' + b"[" * levels + b"0" + b"]" * levels + b"}\n"
        msg, error = decode_line(raw)
        self.assertIsNone(error)
        self.assertEqual(msg["type"], "PING")

    def test_one_level_above_limit_returns_bad_json(self):
        levels = MAX_JSON_DEPTH
        raw = b'{"type":"PING","extra":' + b"[" * levels + b"0" + b"]" * levels + b"}\n"
        self.assertLessEqual(len(raw), MAX_LINE)
        self.assertEqual(decode_line(raw), (None, "BAD_JSON"))

    def test_mixed_object_and_array_depth_is_limited(self):
        nested = b"0"
        for level in range(MAX_JSON_DEPTH):
            nested = b'{"v":' + nested + b"}" if level % 2 else b"[" + nested + b"]"
        raw = b'{"type":"PING","extra":' + nested + b"}\n"
        self.assertLessEqual(len(raw), MAX_LINE)
        self.assertEqual(decode_line(raw), (None, "BAD_JSON"))

    def test_brackets_inside_strings_and_escaped_characters_do_not_count(self):
        msg = {"type": "PING", "nonce": "[{" * MAX_JSON_DEPTH
               + 'quoted " and slash \\' + "}]" * MAX_JSON_DEPTH}
        self.assertEqual(decode_line(encode(msg)), (msg, None))

    def test_escaped_string_cannot_hide_later_deep_container(self):
        prefix = json.dumps({"type": "PING", "nonce": 'quote " and slash \\'},
                            separators=(",", ":")).encode("utf-8")[:-1]
        raw = prefix + b',"extra":' + b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH + b"}\n"
        self.assertEqual(decode_line(raw), (None, "BAD_JSON"))

    def test_sibling_containers_do_not_accumulate_depth(self):
        msg = {"type": "PING", "extra": [{"value": n} for n in range(128)]}
        self.assertEqual(decode_line(encode(msg)), (msg, None))

    def test_unknown_fields_and_types_left_for_dispatch(self):
        msg = {"type": "FUTURE", "future_field": {"nested": True}}
        self.assertEqual(decode_line(encode(msg)), (msg, None))


if __name__ == "__main__":
    unittest.main()
