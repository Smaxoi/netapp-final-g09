"""§13 全部錯誤碼與握手前後的 fatal 判定。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.errors import ERROR_CODES, error_message, is_fatal
from bcproto.wire import decode_line, encode


class ErrorTests(unittest.TestCase):
    def test_exact_15_codes_from_pdf(self):
        self.assertEqual(ERROR_CODES, {
            "BAD_JSON", "LINE_TOO_LONG", "UNKNOWN_TYPE", "BAD_FIELD", "INVALID_VALUE",
            "UNEXPECTED", "NOT_YOUR_TURN", "STALE_TURN", "DUPLICATE",
            "VERSION_UNSUPPORTED", "GAME_IN_PROGRESS", "ROOM_FULL", "NAME_TAKEN",
            "TIMEOUT", "TOO_MANY_ERRORS",
        })

    def test_always_fatal_codes_in_every_context(self):
        for code in ["LINE_TOO_LONG", "VERSION_UNSUPPORTED", "GAME_IN_PROGRESS", "ROOM_FULL",
                     "NAME_TAKEN", "TIMEOUT", "TOO_MANY_ERRORS"]:
            for hello_done in [False, True]:
                for kind in [None, "HELLO", "GUESS"]:
                    with self.subTest(code=code, hello_done=hello_done, kind=kind):
                        self.assertTrue(is_fatal(code, message_type=kind, hello_done=hello_done))

    def test_always_nonfatal_including_bad_json_before_hello(self):
        for code in ["BAD_JSON", "UNKNOWN_TYPE", "NOT_YOUR_TURN", "STALE_TURN", "DUPLICATE"]:
            for hello_done in [False, True]:
                self.assertFalse(is_fatal(code, message_type="HELLO", hello_done=hello_done))

    def test_bad_field_and_value_only_fatal_during_initial_hello(self):
        for code in ["BAD_FIELD", "INVALID_VALUE"]:
            self.assertTrue(is_fatal(code, message_type="HELLO", hello_done=False))
            self.assertFalse(is_fatal(code, message_type="GUESS", hello_done=True))
            self.assertFalse(is_fatal(code, message_type="HELLO", hello_done=True))
            self.assertFalse(is_fatal(code, message_type="CHAT", hello_done=False))

    def test_unexpected_depends_on_hello_completion(self):
        self.assertTrue(is_fatal("UNEXPECTED", message_type="PING", hello_done=False))
        self.assertFalse(is_fatal("UNEXPECTED", message_type="HELLO", hello_done=True))

    def test_error_messages_round_trip_with_computed_fatal(self):
        for code in ERROR_CODES:
            msg = error_message(code, message_type="HELLO")
            self.assertEqual(set(msg), {"type", "code", "message", "fatal"})
            self.assertEqual(msg["code"], code)
            self.assertTrue(msg["message"])
            self.assertIs(type(msg["fatal"]), bool)
            self.assertEqual(decode_line(encode(msg)), (msg, None))
        msg = error_message("NOT_YOUR_TURN", "本回合喊數者為 p2", hello_done=True)
        self.assertEqual(msg["message"], "本回合喊數者為 p2")
        self.assertFalse(msg["fatal"])

    def test_unknown_codes_and_nonstring_messages_rejected(self):
        for fn in [is_fatal, error_message]:
            with self.assertRaises(ValueError):
                fn("TYPO")
        with self.assertRaises(TypeError):
            error_message("BAD_JSON", message=123)


if __name__ == "__main__":
    unittest.main()
