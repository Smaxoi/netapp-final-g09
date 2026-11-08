"""§9 欄位格式與 Unicode 邊界測試。"""

import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.validators import (
    is_int, valid_hex32, valid_hex64, valid_number, valid_text, valid_version,
)


class ValidatorTests(unittest.TestCase):
    def test_all_four_digit_ascii_strings(self):
        count = 0
        for digits in itertools.product("0123456789", repeat=4):
            number = "".join(digits)
            expected = len(set(number)) == 4
            self.assertEqual(valid_number(number), expected, number)
            count += expected
        self.assertEqual(count, 5040)
        self.assertTrue(valid_number("0275"))

    def test_number_rejects_wrong_type_length_and_nonascii(self):
        for value in [1234, True, None, "１２３４", "١٢٣٤", "123", "12345", "12a4", "123\n"]:
            with self.subTest(value=value):
                self.assertFalse(valid_number(value))

    def test_integer_type_is_separate_from_range(self):
        for value in [0, 1, -1]:
            self.assertTrue(is_int(value))
        for value in [True, False, 1.0, "1", None]:
            self.assertFalse(is_int(value))

    def test_unicode_text_boundaries_and_all_c_categories(self):
        self.assertTrue(valid_text("中" * 20))
        self.assertTrue(valid_text("e\u0301"))
        self.assertFalse(valid_text(""))
        self.assertFalse(valid_text("中" * 21))
        self.assertFalse(valid_text(None))
        # Cc, Cf, Cs, Co, Cn 都必須拒絕。
        for char in ["\n", "\x00", "\u200b", "\ud800", "\ue000", "\u0378"]:
            with self.subTest(char=repr(char)):
                self.assertFalse(valid_text("a" + char))

    def test_chat_uses_its_own_limit(self):
        self.assertTrue(valid_text("中" * 200, 1, 200))
        self.assertFalse(valid_text("中" * 201, 1, 200))

    def test_lowercase_hex_exact_lengths(self):
        for validator, size in [(valid_hex32, 32), (valid_hex64, 64)]:
            self.assertTrue(validator("0123456789abcdef" * (size // 16)))
            for value in ["A" * size, "g" * size, "a" * (size - 1), "a" * (size + 1), "a" * size + "\n", None]:
                with self.subTest(size=size, value=value):
                    self.assertFalse(validator(value))

    def test_protocol_accepts_all_minor_versions(self):
        for value in ["BC/1.0", "BC/1.999", "BC/1.00"]:
            self.assertTrue(valid_version(value))
        for value in ["BC/2.0", "BC/1.", "BC/1.０", "BC/1.0\n", "bc/1.0", None]:
            self.assertFalse(valid_version(value))


if __name__ == "__main__":
    unittest.main()
