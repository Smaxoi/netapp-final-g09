"""以 PDF §9、§11、§13 與 §15 範例驗證 A4/A5。"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.errors import ProtocolError
from bcproto.messages import (
    CLIENT_TYPES, SERVER_TYPES, build_client_message, check_client_fields,
    check_client_values, commit, feedback, guess, hello, pong, reveal,
    validate_client_message, validate_server_message,
)
from bcproto.wire import decode_line, encode

SECRET = "0275"
SALT = "2eb6594c74b9b9d71e2d9b5bd14300df"
COMMIT = "6b05c8fe4113aa24833fcbd089d33f0fe48ff68eb0a7d7fff87319a394338721"
PLAYERS = [
    {"id": "p1", "name": "G03-AI", "team": "G03", "kind": "ai"},
    {"id": "p2", "name": "阿明", "team": "G09", "kind": "human"},
]
RULES = {"guess_timeout": 30, "feedback_timeout": 30.5, "commit_timeout": 30,
         "reveal_timeout": 15, "max_turns": 120, "max_skips": 3}


def server_examples():
    return [
        {"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
        {"type": "LOBBY", "players": copy.deepcopy(PLAYERS), "min_players": 2, "max_players": 3},
        {"type": "GAME_START", "game_id": "3d377330a7e0", "players": copy.deepcopy(PLAYERS),
         "order": ["p1", "p2"], "rules": RULES.copy()},
        {"type": "COMMITS", "order": ["p1", "p2"], "commits": {"p1": COMMIT, "p2": COMMIT}},
        {"type": "TURN", "turn_id": 1, "guesser": "p1", "alive": ["p1", "p2"]},
        {"type": "GUESS_MADE", "turn_id": 1, "guesser": "p1", "guess": SECRET, "responders": ["p2"]},
        {"type": "TURN_RESULT", "turn_id": 1, "guesser": "p1", "guess": SECRET, "skipped": False,
         "results": [{"player": "p2", "a": 4, "b": 0}], "eliminated": ["p2"],
         "forfeited": [], "disqualified": []},
        {"type": "REVEAL_REQUEST", "reason": "ELIMINATED"},
        {"type": "REVEALED", "player": "p2", "secret": SECRET, "salt": SALT,
         "verdict": "OK", "bad_turns": []},
        {"type": "GAME_OVER", "game_id": "3d377330a7e0", "reason": "LAST_PLAYER", "turns": 1,
         "ranking": [{"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1},
                     {"player": "p2", "status": "GUESSED", "out_turn": 1, "rank": 2}]},
        {"type": "CHAT", "from": "p1", "text": "你好"},
        {"type": "PING", "nonce": "nonce"},
        {"type": "PONG", "nonce": "nonce"},
        {"type": "ERROR", "code": "NOT_YOUR_TURN", "message": "本回合喊數者為 p2", "fatal": False},
    ]


def example(kind):
    return next(msg for msg in server_examples() if msg["type"] == kind)


class ClientMessageTests(unittest.TestCase):
    def test_six_builders_generate_wire_ready_messages(self):
        messages = [hello("G03-AI", "G03", "ai", software="BC-ref/0.9"),
                    commit(COMMIT), guess(3, "0524"), feedback(3, 1, 2),
                    reveal(SECRET, SALT), pong("nonce")]
        for msg in messages:
            with self.subTest(kind=msg["type"]):
                self.assertIsNone(validate_client_message(msg))
                self.assertEqual(decode_line(encode(msg)), (msg, None))
        self.assertEqual(messages[1], {"type": "COMMIT", "commit": COMMIT})
        self.assertEqual(messages[2], {"type": "GUESS", "turn_id": 3, "guess": "0524"})
        self.assertEqual(messages[4], {"type": "REVEAL", "secret": SECRET, "salt": SALT})

    def test_generic_builder_covers_other_three_types(self):
        for kind, fields in [("CHAT", {"text": "中" * 200}), ("PING", {"nonce": ""}), ("BYE", {})]:
            msg = build_client_message(kind, **fields)
            self.assertIsNone(validate_client_message(msg))
        self.assertEqual(len(CLIENT_TYPES), 9)

    def test_optional_strings_and_nonce_preserved_exactly(self):
        self.assertNotIn("software", hello("阿明", "G09", "human"))
        self.assertEqual(pong(), {"type": "PONG"})
        for nonce in ["", "中文", "case-Sensitive\nnonce"]:
            self.assertEqual(pong(nonce), {"type": "PONG", "nonce": nonce})
        with self.assertRaises(ProtocolError) as caught:
            pong(None)
        self.assertEqual(caught.exception.code, "BAD_FIELD")

    def test_missing_required_client_fields(self):
        for msg in [hello("a", "G09", "ai"), commit(COMMIT), guess(1, SECRET),
                    feedback(1, 0, 0), reveal(SECRET, SALT), build_client_message("CHAT", text="a")]:
            for key in set(msg) - {"type"}:
                broken = {k: v for k, v in msg.items() if k != key}
                with self.subTest(kind=msg["type"], missing=key):
                    self.assertEqual(check_client_fields(broken), "BAD_FIELD")

    def test_type_and_value_checks_stay_separate_for_a7(self):
        for msg in [
            {"type": "GUESS", "turn_id": 0, "guess": "1123"},
            {"type": "COMMIT", "commit": "A" * 64},
            {"type": "FEEDBACK", "turn_id": -1, "a": 3, "b": 1},
            {"type": "REVEAL", "secret": "1123", "salt": "A" * 32},
        ]:
            self.assertIsNone(check_client_fields(msg))
            self.assertEqual(check_client_values(msg), "INVALID_VALUE")
        for msg in [
            {"type": "GUESS", "turn_id": True, "guess": SECRET},
            {"type": "GUESS", "turn_id": 1, "guess": 1234},
            {"type": "FEEDBACK", "turn_id": 1, "a": False, "b": 0},
            {"type": "FEEDBACK", "turn_id": 1.0, "a": 0, "b": 0},
        ]:
            self.assertEqual(check_client_fields(msg), "BAD_FIELD")

    def test_hello_type_then_version_then_value(self):
        msg = {"type": "HELLO", "protocol": "BC/2.0", "name": "", "team": "G09", "kind": "ai"}
        self.assertEqual(validate_client_message(msg), "VERSION_UNSUPPORTED")
        msg["kind"] = True
        self.assertEqual(validate_client_message(msg), "BAD_FIELD")
        msg["kind"] = "ai"
        msg["protocol"] = "BC/1.999"
        self.assertEqual(validate_client_message(msg), "INVALID_VALUE")
        msg["name"] = "a"
        self.assertIsNone(validate_client_message(msg))
        msg["protocol"] = 123
        self.assertEqual(validate_client_message(msg), "BAD_FIELD")

    def test_all_14_feedback_combinations_and_invalid_pairs(self):
        valid_count = 0
        for a in range(-1, 6):
            for b in range(-1, 6):
                expected = 0 <= a <= 4 and 0 <= b <= 4 and a + b <= 4 and (a, b) != (3, 1)
                msg = {"type": "FEEDBACK", "turn_id": 1, "a": a, "b": b}
                self.assertEqual(validate_client_message(msg), None if expected else "INVALID_VALUE")
                valid_count += expected
        self.assertEqual(valid_count, 14)

    def test_builder_rejects_invalid_input_with_code(self):
        for fn, code in [
            (lambda: guess(True, SECRET), "BAD_FIELD"),
            (lambda: guess(1, "１２３４"), "INVALID_VALUE"),
            (lambda: feedback(1, 3, 1), "INVALID_VALUE"),
            (lambda: hello("a\u200b", "G09", "human"), "INVALID_VALUE"),
            (lambda: hello("a", "G09\n", "ai"), "INVALID_VALUE"),
            (lambda: build_client_message("CHAT", text="x" * 201), "INVALID_VALUE"),
            (lambda: build_client_message("CHAT", text="a\n"), "INVALID_VALUE"),
        ]:
            with self.subTest(code=code), self.assertRaises(ProtocolError) as caught:
                fn()
            self.assertEqual(caught.exception.code, code)

    def test_unknown_client_fields_do_not_mutate_or_reject_message(self):
        msg = {"type": "GUESS", "turn_id": 1, "guess": SECRET, "future": {"anything": True}}
        original = copy.deepcopy(msg)
        self.assertIsNone(validate_client_message(msg))
        self.assertEqual(msg, original)

    def test_envelope_and_unknown_client_type(self):
        for msg in [None, [], {}, {"type": True}]:
            self.assertEqual(validate_client_message(msg), "BAD_JSON")
        self.assertEqual(validate_client_message({"type": "FUTURE"}), "UNKNOWN_TYPE")


class ServerMessageTests(unittest.TestCase):
    def test_all_14_server_messages_and_wire_round_trip(self):
        self.assertEqual({msg["type"] for msg in server_examples()}, SERVER_TYPES)
        self.assertEqual(len(SERVER_TYPES), 14)
        for msg in server_examples():
            with self.subTest(kind=msg["type"]):
                self.assertIsNone(validate_server_message(msg))
                self.assertEqual(decode_line(encode(msg)), (msg, None))

    def test_every_required_top_level_field(self):
        for msg in server_examples():
            for key in set(msg) - {"type", "nonce"}:
                broken = {k: v for k, v in msg.items() if k != key}
                with self.subTest(kind=msg["type"], missing=key):
                    self.assertEqual(validate_server_message(broken), "BAD_FIELD")

    def test_nested_required_fields_and_json_types(self):
        cases = []
        for key in ["id", "name", "team", "kind"]:
            msg = example("LOBBY")
            del msg["players"][0][key]
            cases.append(msg)
        for key in RULES:
            msg = example("GAME_START")
            del msg["rules"][key]
            cases.append(msg)
        for key in ["player", "a", "b"]:
            msg = example("TURN_RESULT")
            del msg["results"][0][key]
            cases.append(msg)
        for key in ["player", "status", "out_turn", "rank"]:
            msg = example("GAME_OVER")
            del msg["ranking"][0][key]
            cases.append(msg)
        for kind, key, value in [
            ("TURN", "turn_id", True), ("TURN_RESULT", "skipped", 0),
            ("ERROR", "fatal", 1), ("PING", "nonce", None),
            ("PONG", "nonce", 1), ("LOBBY", "players", {}),
            ("TURN", "alive", [1]), ("REVEALED", "bad_turns", [True]),
        ]:
            msg = example(kind)
            msg[key] = value
            cases.append(msg)
        msg = example("GAME_START")
        msg["rules"]["guess_timeout"] = True
        cases.append(msg)
        msg = example("GAME_OVER")
        msg["ranking"][0]["rank"] = True
        cases.append(msg)
        msg = example("TURN_RESULT")
        msg["results"][0]["a"] = False
        cases.append(msg)
        for msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(validate_server_message(msg), "BAD_FIELD")

    def test_type_errors_take_priority_over_value_errors(self):
        msg = example("GAME_START")
        msg["game_id"] = "invalid id"
        msg["rules"]["max_turns"] = True
        self.assertEqual(validate_server_message(msg), "BAD_FIELD")
        msg = example("WELCOME")
        msg["protocol"] = "BC/2.0"
        msg["player_id"] = 1
        self.assertEqual(validate_server_message(msg), "BAD_FIELD")

    def test_unknown_fields_at_all_object_levels_are_ignored(self):
        msg = example("GAME_START")
        msg["future"] = {"arbitrary": True}
        msg["players"][0]["future"] = None
        msg["rules"]["future"] = {"other": []}
        original = copy.deepcopy(msg)
        self.assertIsNone(validate_server_message(msg))
        self.assertEqual(msg, original)
        self.assertIsNone(validate_server_message({"type": "FUTURE", "arbitrary": True}))

    def test_optional_server_fields_and_protocol(self):
        for kind in ["PING", "PONG"]:
            self.assertIsNone(validate_server_message({"type": kind}))
            self.assertIsNone(validate_server_message({"type": kind, "nonce": ""}))
        msg = example("WELCOME")
        msg.update(protocol="BC/1.999", server="BC-ref/0.9")
        self.assertIsNone(validate_server_message(msg))
        msg["server"] = None
        self.assertEqual(validate_server_message(msg), "BAD_FIELD")
        del msg["server"]
        msg["protocol"] = "BC/2.0"
        self.assertEqual(validate_server_message(msg), "VERSION_UNSUPPORTED")

    def test_field_value_boundaries(self):
        for kind, key, value in [
            ("WELCOME", "player_id", "p0"), ("WELCOME", "player_id", "p１"),
            ("GAME_START", "game_id", "a" * 65), ("GAME_START", "game_id", "含空白 id"),
            ("TURN", "turn_id", 0), ("CHAT", "text", "中" * 201),
            ("CHAT", "text", "a\ue000"), ("ERROR", "code", "TYPO"),
            ("REVEAL_REQUEST", "reason", "GAME_END_typo"),
        ]:
            msg = example(kind)
            msg[key] = value
            with self.subTest(kind=kind, key=key, value=value):
                self.assertEqual(validate_server_message(msg), "INVALID_VALUE")
        for value in [0, -1, float("nan"), float("inf"), float("-inf")]:
            msg = example("GAME_START")
            msg["rules"]["guess_timeout"] = value
            self.assertEqual(validate_server_message(msg), "INVALID_VALUE")

    def test_local_list_relationships(self):
        mutations = [
            ("LOBBY", lambda m: m.update(min_players=1)),
            ("LOBBY", lambda m: m["players"].append(copy.deepcopy(m["players"][0]))),
            ("GAME_START", lambda m: m.update(order=["p2", "p1"])),
            ("COMMITS", lambda m: m["commits"].pop("p2")),
            ("TURN", lambda m: m.update(alive=["p2"])),
            ("GUESS_MADE", lambda m: m.update(responders=["p1", "p2"])),
            ("TURN_RESULT", lambda m: m["results"][0].update(a=3, b=1)),
            ("TURN_RESULT", lambda m: m.update(forfeited=["p2"])),
        ]
        for kind, mutate in mutations:
            msg = example(kind)
            mutate(msg)
            with self.subTest(kind=kind, msg=msg):
                self.assertEqual(validate_server_message(msg), "INVALID_VALUE")

    def test_skipped_turn_and_empty_results(self):
        msg = example("TURN_RESULT")
        msg.update(guess=None, skipped=True, results=[], eliminated=[])
        self.assertIsNone(validate_server_message(msg))
        msg["skipped"] = False
        self.assertEqual(validate_server_message(msg), "INVALID_VALUE")

    def test_reveal_verdict_nulls_and_bad_turns(self):
        for verdict in ["OK", "BAD_COMMIT", "INCONSISTENT", "NO_REVEAL"]:
            msg = example("REVEALED")
            msg["verdict"] = verdict
            msg["bad_turns"] = [1, 3] if verdict == "INCONSISTENT" else []
            if verdict == "NO_REVEAL":
                msg.update(secret=None, salt=None)
            self.assertIsNone(validate_server_message(msg))
            msg["bad_turns"] = [] if verdict == "INCONSISTENT" else [1]
            self.assertEqual(validate_server_message(msg), "INVALID_VALUE")
        msg = example("REVEALED")
        msg["salt"] = None
        self.assertEqual(validate_server_message(msg), "INVALID_VALUE")

    def test_game_over_aborted_ties_and_disqualification(self):
        msg = {"type": "GAME_OVER", "game_id": "game-1", "reason": "ABORTED", "turns": 0, "ranking": []}
        self.assertIsNone(validate_server_message(msg))
        msg["turns"] = 1
        self.assertEqual(validate_server_message(msg), "INVALID_VALUE")
        msg = example("GAME_OVER")
        msg["ranking"] = [
            {"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1},
            {"player": "p2", "status": "SURVIVOR", "out_turn": None, "rank": 1},
            {"player": "p3", "status": "DQ", "out_turn": 1, "rank": None},
        ]
        self.assertIsNone(validate_server_message(msg))
        msg["ranking"][2]["rank"] = 3
        self.assertEqual(validate_server_message(msg), "INVALID_VALUE")
        msg["ranking"][2]["rank"] = None
        msg["ranking"][0]["out_turn"] = 1
        self.assertEqual(validate_server_message(msg), "INVALID_VALUE")

    def test_bad_envelope(self):
        for msg in [[], None, {}, {"type": 1}]:
            self.assertEqual(validate_server_message(msg), "BAD_JSON")


if __name__ == "__main__":
    unittest.main()
