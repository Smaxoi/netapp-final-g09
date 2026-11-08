"""§8.3 與 A1/A2 的框架及異常輸入測試（不需要第三方套件）。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.wire import MAX_LINE, decode_line, encode


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

    def test_unknown_fields_and_types_left_for_dispatch(self):
        msg = {"type": "FUTURE", "future_field": {"nested": True}}
        self.assertEqual(decode_line(encode(msg)), (msg, None))


if __name__ == "__main__":
    unittest.main()
