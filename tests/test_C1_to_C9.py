"""AI agent C1～C9 的行為測試。

這些測試只模擬 StreamReader/StreamWriter，不需要開真正的 TCP port。
執行：python3 -m unittest tests.test_C1_to_C9 -v
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from bcproto.ai.agent import AIAgent
from bcproto.rules import ALL_NUMBERS, make_commit, score


class FakeReader:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        await asyncio.sleep(0)
        return self.lines.pop(0) if self.lines else b""


class FakeWriter:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, data):
        self.messages.append(json.loads(data.decode("utf-8")))

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)


class C1ToC9Tests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self):
        agent = AIAgent("127.0.0.1", 9999, quiet=True)
        agent.writer = FakeWriter()
        return agent

    def sent_types(self, agent):
        return [message["type"] for message in agent.writer.messages]

    async def test_c1_run_connects_reads_and_dispatches(self):
        reader = FakeReader([
            b'{"type":"WELCOME","protocol":"BC/1.0","player_id":"p1"}\n',
            b'{"type":"UNKNOWN_MESSAGE"}\n',
        ])
        writer = FakeWriter()

        async def fake_open_connection(*args, **kwargs):
            self.assertEqual(kwargs["limit"], 4095)
            return reader, writer

        agent = AIAgent("127.0.0.1", 9999, quiet=True)
        with patch("bcproto.ai.agent.asyncio.open_connection", fake_open_connection):
            await agent.run()

        self.assertEqual(agent.player_id, "p1")
        self.assertEqual(self.sent_types(type("Holder", (), {"writer": writer})()), ["HELLO"])
        self.assertTrue(writer.closed)

    async def test_c2_hello_welcome_remembers_player_id(self):
        agent = self.make_agent()
        await agent.send_hello()
        await agent.handle_wellcome({
            "type": "WELCOME", "protocol": "BC/1.0", "player_id": "p7"
        })
        self.assertEqual(agent.player_id, "p7")
        self.assertEqual(agent.writer.messages[0]["type"], "HELLO")

    async def test_c3_ping_returns_matching_pong_and_ignores_unknown_type(self):
        agent = self.make_agent()
        await agent.dispatch({"type": "PING", "nonce": "abc"})
        await agent.dispatch({"type": "SOMETHING_ELSE", "value": 1})
        self.assertEqual(agent.writer.messages, [{"type": "PONG", "nonce": "abc"}])

    async def test_c4_game_start_sends_valid_commit(self):
        agent = self.make_agent()
        agent.player_id = "p1"
        await agent.dispatch({
            "type": "GAME_START",
            "players": ["p1", "p2"],
        })
        message = agent.writer.messages[0]
        self.assertEqual(message["type"], "COMMIT")
        self.assertRegex(agent.secret, r"^[0-9]{4}$")
        self.assertEqual(len(set(agent.secret)), 4)
        self.assertEqual(message["commit"], make_commit(agent.secret, agent.salt))

    async def test_c5_turn_sends_guess_for_self_only(self):
        agent = self.make_agent()
        agent.player_id = "p1"
        agent.candidates = {"p2": ["4917"]}
        await agent.dispatch({"type": "TURN", "turn_id": 3, "guesser": "p2"})
        self.assertEqual(agent.writer.messages, [])
        await agent.dispatch({"type": "TURN", "turn_id": 3, "guesser": "p1"})
        message = agent.writer.messages[0]
        self.assertEqual(message, {"type": "GUESS", "turn_id": 3, "guess": "4917"})

    async def test_c6_guess_made_sends_correct_feedback(self):
        agent = self.make_agent()
        agent.player_id = "p1"
        agent.secret = "1234"
        await agent.dispatch({
            "type": "GUESS_MADE",
            "turn_id": 4,
            "guesser": "p2",
            "guess": "1034",
            "responders": ["p1"],
        })
        self.assertEqual(agent.writer.messages, [{
            "type": "FEEDBACK", "turn_id": 4, "a": 3, "b": 0
        }])

    async def test_c7_reveal_request_sends_secret_and_salt_once(self):
        agent = self.make_agent()
        agent.secret, agent.salt = "1234", "ab" * 16
        request = {"type": "REVEAL_REQUEST", "reason": "GAME_END"}
        await agent.dispatch(request)
        await agent.dispatch(request)
        self.assertEqual(agent.writer.messages, [{
            "type": "REVEAL", "secret": "1234", "salt": "ab" * 16
        }])

    async def test_c8_turn_result_updates_every_opponent(self):
        agent = self.make_agent()
        agent.player_id = "p1"
        await agent.handle_game_start({
            "players": ["p1", "p2", "p3"],
        })
        await agent.dispatch({
            "type": "TURN_RESULT", "turn_id": 1, "guess": "1234",
            "results": [
                {"player": "p2", "a": 4, "b": 0},
                {"player": "p3", "a": 0, "b": 0},
            ],
        })
        self.assertEqual(agent.candidates["p2"], ["1234"])
        self.assertEqual(
            agent.candidates["p3"],
            [candidate for candidate in ALL_NUMBERS if score("1234", candidate) == (0, 0)],
        )
        self.assertNotIn("p1", agent.candidates)

    async def test_c9_full_automatic_event_sequence(self):
        agent = self.make_agent()
        agent.player_id = "p1"
        await agent.dispatch({"type": "GAME_START", "players": ["p1", "p2"]})
        await agent.dispatch({"type": "TURN", "turn_id": 1, "guesser": "p1"})
        await agent.dispatch({
            "type": "GUESS_MADE", "turn_id": 2, "guesser": "p2",
            "guess": "5678", "responders": ["p1"],
        })
        await agent.dispatch({
            "type": "TURN_RESULT", "turn_id": 2, "guess": "5678",
            "results": [{"player": "p1", "a": 0, "b": 1}],
        })
        await agent.dispatch({"type": "REVEAL_REQUEST", "reason": "GAME_END"})

        self.assertEqual(
            self.sent_types(agent), ["COMMIT", "GUESS", "FEEDBACK", "REVEAL"]
        )


if __name__ == "__main__":
    unittest.main()
