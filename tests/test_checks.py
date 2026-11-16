"""A7：PDF §13 的優先順序測試，刻意讓同一則訊息同時犯多個錯誤。"""

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.server.checks import check_message

COMMIT = "a" * 64
SALT = "b" * 32


class CheckOrderTests(unittest.TestCase):
    def setUp(self):
        self.p1 = SimpleNamespace(pid="p1", name="one", status="ALIVE", commit=None, reveal=None)
        self.p2 = SimpleNamespace(pid="p2", name="two", status="ALIVE", commit=None, reveal=None)
        self.server = SimpleNamespace(
            a=SimpleNamespace(players=4), phase="PLAY", players={"p1": self.p1, "p2": self.p2},
            turn_id=3, guesser="p1", guess_open=True, fb_open=True,
            responders=["p2"], feedback={}, reveal_wait={"p1", "p2"},
        )

    def check(self, msg, player=None):
        before = copy.deepcopy(self.server)
        code = check_message(msg, self.server, player)
        self.assertEqual(self.server, before, "檢查不可更動遊戲狀態")
        return code

    def test_bad_envelope_before_and_after_hello(self):
        for msg in [None, [], {}, {"type": 1}, {"type": True}]:
            for player in [None, self.p1]:
                with self.subTest(msg=msg, player=player):
                    self.assertEqual(self.check(msg, player), "BAD_JSON")

    def test_nonhello_before_handshake_always_unexpected(self):
        for kind in ["PING", "PONG", "BYE", "GUESS", "COMMIT", "CHAT", "FUTURE", ""]:
            self.assertEqual(self.check({"type": kind}), "UNEXPECTED")

    def test_repeated_hello_precedes_field_validation(self):
        self.assertEqual(self.check({"type": "HELLO"}, self.p1), "UNEXPECTED")

    def test_hello_all_six_errors_in_order(self):
        self.server.phase = "PLAY"
        self.server.a.players = 2
        msg = {"type": "HELLO", "protocol": "BC/2.0", "name": "one", "team": "", "kind": True}
        self.assertEqual(self.check(msg), "BAD_FIELD")
        msg["kind"] = "ai"
        self.assertEqual(self.check(msg), "VERSION_UNSUPPORTED")
        msg["protocol"] = "BC/1.999"
        self.assertEqual(self.check(msg), "INVALID_VALUE")
        msg["team"] = "G09"
        self.assertEqual(self.check(msg), "GAME_IN_PROGRESS")
        self.server.phase = "LOBBY"
        self.assertEqual(self.check(msg), "ROOM_FULL")
        self.server.a.players = 3
        self.assertEqual(self.check(msg), "NAME_TAKEN")
        msg["name"] = "new"
        self.assertIsNone(self.check(msg))

    def test_hello_optional_software_type_is_checked_first(self):
        self.server.phase = "LOBBY"
        msg = {"type": "HELLO", "protocol": "BC/2.0", "name": "new",
               "team": "G09", "kind": "ai", "software": None}
        self.assertEqual(self.check(msg), "BAD_FIELD")

    def test_hello_names_are_case_sensitive_and_not_normalized(self):
        self.server.phase = "LOBBY"
        for name in ["ONE", "é"]:
            self.p1.name = "one" if name == "ONE" else "e\u0301"
            msg = {"type": "HELLO", "protocol": "BC/1.0", "name": name, "team": "G09", "kind": "human"}
            self.assertIsNone(self.check(msg))

    def test_commit_order_fields_phase_duplicate_value(self):
        self.p1.commit = COMMIT
        msg = {"type": "COMMIT", "commit": 123}
        self.assertEqual(self.check(msg, self.p1), "BAD_FIELD")
        msg["commit"] = "invalid"
        self.assertEqual(self.check(msg, self.p1), "UNEXPECTED")
        self.server.phase = "COMMIT"
        self.assertEqual(self.check(msg, self.p1), "DUPLICATE")
        self.p1.commit = None
        self.assertEqual(self.check(msg, self.p1), "INVALID_VALUE")
        msg["commit"] = COMMIT
        self.assertIsNone(self.check(msg, self.p1))

    def test_commit_requires_actual_active_participant(self):
        self.server.phase = "COMMIT"
        self.p1.commit = COMMIT
        for status in ["LOBBY", "REMOVED", "GUESSED", "FORFEIT", "DQ"]:
            self.p1.status = status
            self.assertEqual(self.check({"type": "COMMIT", "commit": "invalid"}, self.p1), "UNEXPECTED")
        self.p1.status = "ALIVE"
        del self.server.players["p1"]
        self.assertEqual(self.check({"type": "COMMIT", "commit": COMMIT}, self.p1), "UNEXPECTED")

    def test_guess_order_all_six_steps(self):
        self.server.phase = "COMMIT"
        self.server.guess_open = False
        msg = {"type": "GUESS", "turn_id": True, "guess": "1123"}
        self.assertEqual(self.check(msg, self.p2), "BAD_FIELD")
        msg["turn_id"] = 0
        self.assertEqual(self.check(msg, self.p2), "UNEXPECTED")
        self.server.phase = "PLAY"
        self.assertEqual(self.check(msg, self.p2), "STALE_TURN")
        msg["turn_id"] = 3
        self.assertEqual(self.check(msg, self.p2), "NOT_YOUR_TURN")
        self.assertEqual(self.check(msg, self.p1), "UNEXPECTED")
        self.server.guess_open = True
        self.assertEqual(self.check(msg, self.p1), "INVALID_VALUE")
        msg["guess"] = "0275"
        self.assertIsNone(self.check(msg, self.p1))

    def test_guess_old_and_future_turns_precede_bad_guess(self):
        for turn in [-1, 0, 2, 4, 10000]:
            self.assertEqual(self.check({"type": "GUESS", "turn_id": turn, "guess": "bad"}, self.p1), "STALE_TURN")

    def test_feedback_order_all_six_steps(self):
        self.server.phase = "COMMIT"
        self.server.fb_open = False
        self.server.feedback["p2"] = (0, 0)
        msg = {"type": "FEEDBACK", "turn_id": True, "a": 3, "b": 1}
        self.assertEqual(self.check(msg, self.p2), "BAD_FIELD")
        msg["turn_id"] = 0
        self.assertEqual(self.check(msg, self.p2), "UNEXPECTED")
        self.server.phase = "PLAY"
        self.assertEqual(self.check(msg, self.p2), "STALE_TURN")
        msg["turn_id"] = 3
        self.assertEqual(self.check(msg, self.p2), "UNEXPECTED")
        self.server.fb_open = True
        self.assertEqual(self.check(msg, self.p2), "DUPLICATE")
        self.server.feedback.clear()
        self.assertEqual(self.check(msg, self.p2), "INVALID_VALUE")
        msg.update(a=1, b=2)
        self.assertIsNone(self.check(msg, self.p2))

    def test_nonresponder_and_closed_feedback_precede_duplicate(self):
        msg = {"type": "FEEDBACK", "turn_id": 3, "a": 3, "b": 1}
        self.server.feedback["p1"] = (0, 0)
        self.assertEqual(self.check(msg, self.p1), "UNEXPECTED")
        self.server.feedback["p2"] = (0, 0)
        self.server.fb_open = False
        self.assertEqual(self.check(msg, self.p2), "UNEXPECTED")

    def test_feedback_boolean_and_float_fields_are_bad_field(self):
        self.server.phase = "LOBBY"
        for key in ["turn_id", "a", "b"]:
            for value in [True, False, 1.0]:
                msg = {"type": "FEEDBACK", "turn_id": 3, "a": 1, "b": 2, key: value}
                self.assertEqual(self.check(msg, self.p2), "BAD_FIELD")

    def test_reveal_fields_then_duplicate_then_value(self):
        self.p1.reveal = ("0275", SALT)
        self.server.reveal_wait.clear()
        msg = {"type": "REVEAL", "secret": 1234, "salt": "bad"}
        self.assertEqual(self.check(msg, self.p1), "BAD_FIELD")
        msg["secret"] = "1123"
        self.assertEqual(self.check(msg, self.p1), "DUPLICATE")
        self.p1.reveal = None
        self.assertEqual(self.check(msg, self.p1), "UNEXPECTED")
        self.server.reveal_wait.add("p1")
        self.assertEqual(self.check(msg, self.p1), "INVALID_VALUE")
        msg.update(secret="0275", salt=SALT)
        self.assertIsNone(self.check(msg, self.p1))

    def test_reveal_valid_format_does_not_check_honesty(self):
        self.server.phase = "END"
        self.p1.status = "GUESSED"
        self.p1.commit = "0" * 64  # 刻意不符合揭示，這是 B14 的 BAD_COMMIT。
        self.assertIsNone(self.check({"type": "REVEAL", "secret": "0275", "salt": SALT}, self.p1))

    def test_invalid_reveal_can_be_corrected_without_state_change(self):
        self.assertEqual(self.check({"type": "REVEAL", "secret": "1123", "salt": SALT}, self.p1), "INVALID_VALUE")
        self.assertIn("p1", self.server.reveal_wait)
        self.assertIsNone(self.p1.reveal)
        self.assertIsNone(self.check({"type": "REVEAL", "secret": "0275", "salt": SALT}, self.p1))

    def test_chat_fields_then_values_in_all_post_hello_phases(self):
        for phase in ["LOBBY", "COMMIT", "PLAY", "END"]:
            self.server.phase = phase
            self.assertEqual(self.check({"type": "CHAT"}, self.p1), "BAD_FIELD")
            self.assertEqual(self.check({"type": "CHAT", "text": 123}, self.p1), "BAD_FIELD")
            for text in ["", "x" * 201, "a\n", "a\u200b"]:
                self.assertEqual(self.check({"type": "CHAT", "text": text}, self.p1), "INVALID_VALUE")
            self.assertIsNone(self.check({"type": "CHAT", "text": "中" * 200}, self.p1))

    def test_eliminated_or_disqualified_players_can_still_chat(self):
        for status in ["GUESSED", "FORFEIT", "DQ"]:
            self.p1.status = status
            self.assertIsNone(self.check({"type": "CHAT", "text": "hello"}, self.p1))

    def test_unknown_type_and_other_supported_messages_after_hello(self):
        self.assertEqual(self.check({"type": "FUTURE"}, self.p1), "UNKNOWN_TYPE")
        for kind in ["PING", "PONG", "BYE"]:
            self.assertIsNone(self.check({"type": kind}, self.p1))
        self.assertEqual(self.check({"type": "PING", "nonce": 1}, self.p1), "BAD_FIELD")

    def test_unknown_fields_are_ignored_without_mutating_message(self):
        msg = {"type": "GUESS", "turn_id": 3, "guess": "0275", "future": {"anything": True}}
        before = copy.deepcopy(msg)
        self.assertIsNone(self.check(msg, self.p1))
        self.assertEqual(msg, before)


if __name__ == "__main__":
    unittest.main()
