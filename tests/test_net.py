"""A9～A11：用真正的 TCP 連線驗證 Server、A7 檢查與 A8 傳送器整合。"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.messages import hello, validate_server_message
from bcproto.server import HELLO_TIMEOUT, Server
from bcproto.wire import MAX_LINE, decode_line, encode

COMMIT = "a" * 64
SALT = "b" * 32


class NetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(players=4, log_dir=self.temp.name))
        self.server.open_log("network-test")
        self.tasks = []
        self.clients = []

        def accept(reader, writer):
            task = asyncio.create_task(self.server.handle_conn(reader, writer))
            self.tasks.append(task)

        self.listener = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.listener.close()
        for _, writer in self.clients:
            writer.close()
        for conn in list(self.server.conns):
            conn.abort()
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
        for _, writer in self.clients:
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await asyncio.wait_for(self.listener.wait_closed(), 1)
        self.server.close_log()
        self.temp.cleanup()

    async def client(self):
        pair = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append(pair)
        return pair

    async def send(self, writer, msg):
        writer.write(encode(msg))
        await writer.drain()

    async def receive(self, reader):
        raw = await asyncio.wait_for(reader.readline(), 1)
        self.assertTrue(raw, "預期收到訊息，卻先收到 EOF")
        msg, error = decode_line(raw)
        self.assertIsNone(error)
        self.assertIsNone(validate_server_message(msg), msg)
        return msg

    async def eof(self, reader):
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")

    async def joined(self, name="one"):
        reader, writer = await self.client()
        await self.send(writer, hello(name, "G09", "human"))
        welcome = await self.receive(reader)
        lobby = await self.receive(reader)
        self.assertEqual(welcome["type"], "WELCOME")
        self.assertEqual(lobby["type"], "LOBBY")
        return reader, writer, self.server.players[welcome["player_id"]]

    async def group(self, count=2):
        peers = []
        for n in range(count):
            peer = await self.joined(f"player-{n}")
            for reader, _, _ in peers:
                self.assertEqual((await self.receive(reader))["type"], "LOBBY")
            peers.append(peer)
        return peers

    async def changed_until(self, predicate):
        self.assertTrue(await self.server.wait_until(predicate, 1))

    async def assert_error(self, reader, code, fatal=False):
        msg = await self.receive(reader)
        self.assertEqual(msg["type"], "ERROR")
        self.assertEqual(msg["code"], code)
        self.assertEqual(msg["fatal"], fatal)
        if fatal:
            await self.eof(reader)

    def logs(self):
        path = next(Path(self.temp.name).glob("*.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    async def test_hello_welcome_then_lobby_and_player_contract(self):
        reader, writer, player = await self.joined("阿明")
        self.assertEqual(player.pid, "p1")
        self.assertEqual(player.name, "阿明")
        self.assertEqual(player.team, "G09")
        self.assertEqual(player.status, "LOBBY")
        self.assertEqual(self.server.next_id, 2)
        self.assertIs(player.conn.player, player)
        self.assertIn(player.conn, self.server.conns)
        self.assertEqual(player.conn.label(), "p1")
        self.assertEqual(player.cands[0], "0123")
        self.assertEqual(len(player.cands), 5040)

    async def test_join_order_broadcast_and_no_welcome_to_other_players(self):
        r1, _, p1 = await self.joined("one")
        _, _, p2 = await self.joined("two")
        lobby = await self.receive(r1)
        self.assertEqual(lobby["type"], "LOBBY")
        self.assertEqual([p["id"] for p in lobby["players"]], [p1.pid, p2.pid])

    async def test_fragmented_utf8_and_crlf(self):
        reader, writer = await self.client()
        raw = encode(hello("阿明", "G09", "ai"))[:-1] + b"\r\n"
        split = raw.index("阿".encode("utf-8")) + 1
        writer.write(raw[:split])
        await writer.drain()
        self.assertEqual(self.server.players, {})
        writer.write(raw[split:])
        await writer.drain()
        self.assertEqual((await self.receive(reader))["type"], "WELCOME")
        self.assertEqual((await self.receive(reader))["type"], "LOBBY")

    async def test_blank_bad_json_and_hello_in_same_tcp_write(self):
        reader, writer = await self.client()
        writer.write(b"\n\r\nnot json\n" + encode(hello("one", "G09", "ai")))
        await writer.drain()
        await self.assert_error(reader, "BAD_JSON")
        self.assertEqual((await self.receive(reader))["type"], "WELCOME")
        self.assertEqual((await self.receive(reader))["type"], "LOBBY")

    async def test_exact_4096_bytes_is_accepted(self):
        reader, writer = await self.client()
        msg = hello("one", "G09", "ai")
        msg["future"] = ""
        msg["future"] = "x" * (MAX_LINE - len(encode(msg)))
        raw = encode(msg)
        self.assertEqual(len(raw), MAX_LINE)
        writer.write(raw)
        await writer.drain()
        self.assertEqual((await self.receive(reader))["type"], "WELCOME")
        await self.receive(reader)

    async def test_4097_bytes_with_lf_is_fatal(self):
        reader, writer = await self.client()
        writer.write(b"x" * MAX_LINE + b"\n")
        await writer.drain()
        await self.assert_error(reader, "LINE_TOO_LONG", True)
        self.assertEqual(self.server.next_id, 1)

    async def test_oversized_unterminated_line_is_rejected_without_waiting_for_lf(self):
        reader, writer = await self.client()
        writer.write(b"x" * (MAX_LINE + 1))
        await writer.drain()
        await self.assert_error(reader, "LINE_TOO_LONG", True)

    async def test_limit_counts_utf8_bytes(self):
        reader, writer = await self.client()
        msg = {**hello("one", "G09", "ai"), "future": "中" * 1400}
        text = json.dumps(msg, ensure_ascii=False)
        self.assertLess(len(text), MAX_LINE)
        self.assertGreater(len(text.encode("utf-8")), MAX_LINE)
        writer.write(text.encode("utf-8") + b"\n")
        await writer.drain()
        await self.assert_error(reader, "LINE_TOO_LONG", True)

    async def test_incomplete_hello_at_eof_is_not_accepted(self):
        reader, writer = await self.client()
        writer.write(encode(hello("one", "G09", "ai"))[:-1])
        await writer.drain()
        writer.write_eof()
        await self.assert_error(reader, "BAD_JSON")
        await self.eof(reader)
        self.assertEqual(self.server.players, {})
        self.assertEqual(self.server.next_id, 1)

    async def test_hello_default_deadline_is_ten_seconds(self):
        self.assertEqual(HELLO_TIMEOUT, 10.0)

    async def test_silent_connection_times_out_and_is_removed(self):
        with patch("bcproto.server.HELLO_TIMEOUT", 0.05):
            reader, _ = await self.client()
            await self.assert_error(reader, "TIMEOUT", True)
            await self.changed_until(lambda: not self.server.conns)

    async def test_bad_json_does_not_reset_hello_deadline(self):
        with patch("bcproto.server.HELLO_TIMEOUT", 0.20), patch.object(
                self.server, "_read_chunk", wraps=self.server._read_chunk) as read_chunk:
            reader, writer = await self.client()
            await self.changed_until(lambda: len(self.server.conns) == 1)
            await asyncio.sleep(0.04)
            writer.write(b"not json\n\n")
            await writer.drain()
            await self.assert_error(reader, "BAD_JSON")
            await self.assert_error(reader, "TIMEOUT", True)
            timeouts = [call.args[3] for call in read_chunk.call_args_list]
            self.assertGreaterEqual(len(timeouts), 2)
            self.assertLess(timeouts[1], timeouts[0] - 0.025)

    async def test_completed_hello_disables_handshake_timer(self):
        with patch("bcproto.server.HELLO_TIMEOUT", 0.05):
            reader, writer, _ = await self.joined()
            await asyncio.sleep(0.07)
            await self.send(writer, {"type": "FUTURE"})
            await self.assert_error(reader, "UNKNOWN_TYPE")

    async def test_nonhello_before_handshake_is_fatal_even_when_malformed(self):
        for msg in [{"type": "PING"}, {"type": "GUESS"}, {"type": "FUTURE"}]:
            reader, writer = await self.client()
            await self.send(writer, msg)
            await self.assert_error(reader, "UNEXPECTED", True)
        self.assertEqual(self.server.players, {})

    async def test_hello_error_priority_and_no_ids_consumed(self):
        for msg, code in [
            ({**hello("one", "G09", "ai"), "protocol": 2}, "BAD_FIELD"),
            ({**hello("one", "G09", "ai"), "protocol": "BC/2.0", "name": ""}, "VERSION_UNSUPPORTED"),
            ({**hello("one", "G09", "ai"), "name": ""}, "INVALID_VALUE"),
        ]:
            reader, writer = await self.client()
            await self.send(writer, msg)
            await self.assert_error(reader, code, True)
            self.assertEqual(self.server.next_id, 1)
            self.assertEqual(self.server.players, {})

    async def test_room_full_precedes_name_taken(self):
        self.server.a.players = 2
        r1, _, _ = await self.joined("one")
        await self.joined("two")
        await self.receive(r1)
        reader, writer = await self.client()
        await self.send(writer, hello("one", "G09", "human"))
        await self.assert_error(reader, "ROOM_FULL", True)
        self.assertEqual(self.server.next_id, 3)

    async def test_game_in_progress_precedes_full_room_and_name(self):
        await self.joined("one")
        self.server.a.players = 1
        self.server.phase = "COMMIT"
        reader, writer = await self.client()
        await self.send(writer, hello("one", "G09", "human"))
        await self.assert_error(reader, "GAME_IN_PROGRESS", True)

    async def test_duplicate_name_fatal_and_repeated_hello_nonfatal(self):
        r1, w1, player = await self.joined("one")
        r2, w2 = await self.client()
        await self.send(w2, hello("one", "G09", "human"))
        await self.assert_error(r2, "NAME_TAKEN", True)
        await self.send(w1, {"type": "HELLO"})
        await self.assert_error(r1, "UNEXPECTED")
        self.assertFalse(player.conn.closed)
        self.assertEqual(self.server.next_id, 2)

    async def test_commit_accepted_only_once_and_engine_is_woken(self):
        reader, writer, player = await self.joined()
        self.server.phase = "COMMIT"
        player.status = "ALIVE"
        await self.send(writer, {"type": "COMMIT", "commit": COMMIT})
        await self.changed_until(lambda: player.commit is not None)
        self.assertEqual(player.commit, COMMIT)
        self.assertEqual(player.status, "ALIVE")
        await self.send(writer, {"type": "COMMIT", "commit": "invalid"})
        await self.assert_error(reader, "DUPLICATE")
        self.assertEqual(player.commit, COMMIT)

    async def test_guess_closes_window_and_second_guess_cannot_replace_it(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.guesser = player.pid
        self.server.guess_open = True
        writer.write(encode({"type": "GUESS", "turn_id": 3, "guess": "0275"})
                     + encode({"type": "GUESS", "turn_id": 3, "guess": "1234"}))
        await writer.drain()
        await self.assert_error(reader, "UNEXPECTED")
        self.assertEqual(self.server.cur_guess, "0275")
        self.assertFalse(self.server.guess_open)

    async def test_guess_stale_turn_precedes_invalid_number_without_state_change(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.guesser = player.pid
        self.server.guess_open = True
        await self.send(writer, {"type": "GUESS", "turn_id": 2, "guess": "1123"})
        await self.assert_error(reader, "STALE_TURN")
        self.assertIsNone(self.server.cur_guess)
        self.assertTrue(self.server.guess_open)
        await self.send(writer, {"type": "GUESS", "turn_id": True, "guess": "0275"})
        await self.assert_error(reader, "BAD_FIELD")

    async def test_feedback_fills_existing_dict_without_publishing_results(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.fb_open = True
        self.server.responders = [player.pid]
        feedback_ref = self.server.feedback
        await self.send(writer, {"type": "FEEDBACK", "turn_id": 3, "a": 1, "b": 2})
        await self.changed_until(lambda: player.pid in self.server.feedback)
        self.assertIs(self.server.feedback, feedback_ref)
        self.assertEqual(self.server.feedback, {player.pid: (1, 2)})
        self.assertEqual(player.records, [])
        self.assertEqual(len(player.cands), 5040)
        await self.send(writer, {"type": "FEEDBACK", "turn_id": 3, "a": 3, "b": 1})
        await self.assert_error(reader, "DUPLICATE")
        sent_types = [row["msg"]["type"] for row in self.logs() if row["dir"] == "send"]
        self.assertEqual(sent_types, ["WELCOME", "LOBBY", "ERROR"])

    async def test_invalid_feedback_can_be_corrected(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.fb_open = True
        self.server.responders = [player.pid]
        await self.send(writer, {"type": "FEEDBACK", "turn_id": 3, "a": 3, "b": 1})
        await self.assert_error(reader, "INVALID_VALUE")
        self.assertEqual(self.server.feedback, {})
        await self.send(writer, {"type": "FEEDBACK", "turn_id": 3, "a": 4, "b": 0})
        await self.changed_until(lambda: player.pid in self.server.feedback)
        self.assertEqual(self.server.feedback[player.pid], (4, 0))

    async def test_reveal_only_discards_own_wait_entry_and_can_retry_format(self):
        reader, writer, player = await self.joined()
        self.server.phase = "END"
        self.server.reveal_wait.update([player.pid, "p2"])
        wait_ref = self.server.reveal_wait
        await self.send(writer, {"type": "REVEAL", "secret": "1123", "salt": SALT})
        await self.assert_error(reader, "INVALID_VALUE")
        self.assertIsNone(player.reveal)
        self.assertIn(player.pid, wait_ref)
        await self.send(writer, {"type": "REVEAL", "secret": "0275", "salt": SALT})
        await self.changed_until(lambda: player.reveal is not None)
        self.assertEqual(player.reveal, ("0275", SALT))
        self.assertIs(self.server.reveal_wait, wait_ref)
        self.assertEqual(wait_ref, {"p2"})
        await self.send(writer, {"type": "REVEAL", "secret": "1234", "salt": SALT})
        await self.assert_error(reader, "DUPLICATE")
        self.assertEqual(player.reveal, ("0275", SALT))

    async def test_lobby_disconnect_broadcasts_removal_and_ids_are_not_reused(self):
        r1, w1, p1 = await self.joined("one")
        r2, _, p2 = await self.joined("two")
        await self.receive(r1)
        w1.close()
        await w1.wait_closed()
        lobby = await self.receive(r2)
        self.assertEqual([p["id"] for p in lobby["players"]], [p2.pid])
        self.assertNotIn(p1.pid, self.server.players)
        _, _, p3 = await self.joined("three")
        self.assertEqual(p3.pid, "p3")

    async def test_play_disconnect_keeps_state_for_engine_decision(self):
        _, writer, player = await self.joined()
        self.server.phase = "PLAY"
        player.status = "ALIVE"
        self.server.reveal_wait.add(player.pid)
        writer.close()
        await writer.wait_closed()
        await self.changed_until(lambda: player.conn.closed)
        self.assertIn(player.pid, self.server.players)
        self.assertEqual(player.status, "ALIVE")
        self.assertIsNone(player.out_turn)
        self.assertIn(player.pid, self.server.reveal_wait)

    async def test_close_all_conns_wakes_readers_including_unjoined_connections(self):
        reader, _ = await self.client()
        await self.changed_until(lambda: len(self.server.conns) == 1)
        self.server.close_all_conns()
        await self.eof(reader)
        await self.changed_until(lambda: not self.server.conns)

    async def test_cancelling_receive_task_cleans_up_connection_and_player(self):
        reader, _, player = await self.joined()
        self.tasks[-1].cancel()
        await asyncio.wait_for(asyncio.gather(self.tasks[-1], return_exceptions=True), 1)
        await self.eof(reader)
        self.assertNotIn(player.pid, self.server.players)
        self.assertEqual(self.server.conns, set())
        self.assertTrue(player.conn.closed)

    async def test_game_over_ignores_even_bad_input(self):
        reader, writer, player = await self.joined()
        self.server.phase = "OVER"
        writer.write(b"not json\n")
        await writer.drain()
        await self.eof(reader)
        self.assertIn(player.pid, self.server.players)
        self.assertEqual([row["msg"]["type"] for row in self.logs() if row["dir"] == "send"], ["WELCOME", "LOBBY"])

    async def test_receive_and_send_logs_use_ip_then_player_id(self):
        reader, writer, _ = await self.joined("阿明")
        await self.send(writer, {"type": "FUTURE"})
        await self.assert_error(reader, "UNKNOWN_TYPE")
        records = self.logs()
        self.assertEqual(records[0]["dir"], "recv")
        self.assertTrue(records[0]["peer"].startswith("127.0.0.1:"))
        self.assertEqual(records[0]["msg"]["name"], "阿明")
        self.assertEqual([row["peer"] for row in records[1:]], ["p1"] * (len(records) - 1))
        self.assertEqual(sum(row["dir"] == "send" and row["msg"]["type"] == "WELCOME" for row in records), 1)

    async def test_unparseable_input_is_logged_as_raw(self):
        reader, writer = await self.client()
        writer.write(b"\xff\n")
        await writer.drain()
        await self.assert_error(reader, "BAD_JSON")
        record = self.logs()[0]
        self.assertEqual(record["dir"], "recv")
        self.assertIn("raw", record["msg"])

    async def test_reader_exception_is_isolated_and_connection_is_cleaned_up(self):
        _, _, player = await self.joined()
        peer = next(iter(self.server.conns))
        with patch.object(self.server, "dispatch", side_effect=RuntimeError("one broken handler")):
            reader, writer = await self.client()
            await self.send(writer, hello("two", "G09", "ai"))
            await self.eof(reader)
        self.assertFalse(player.conn.closed)
        self.assertIn(peer, self.server.conns)

    async def test_ping_echoes_nonce_exactly_and_omits_absent_nonce(self):
        reader, writer, player = await self.joined()
        for fields in [{}, {"nonce": ""}, {"nonce": "Case-Sensitive"}, {"nonce": "中文\n\u200b"}]:
            await self.send(writer, {"type": "PING", **fields})
            self.assertEqual(await self.receive(reader), {"type": "PONG", **fields})
        self.assertEqual(player.conn.error_count, 0)

    async def test_pong_is_ignored_and_does_not_create_reply_loop(self):
        reader, writer, player = await self.joined()
        writer.write(encode({"type": "PONG", "nonce": "already-replied"})
                     + encode({"type": "PONG"})
                     + encode({"type": "PING", "nonce": "barrier"}))
        await writer.drain()
        self.assertEqual(await self.receive(reader), {"type": "PONG", "nonce": "barrier"})
        self.assertEqual(player.conn.error_count, 0)

    async def test_ping_and_pong_nonce_must_be_string(self):
        reader, writer, player = await self.joined()
        for kind in ["PING", "PONG"]:
            for nonce in [None, 1, True]:
                await self.send(writer, {"type": kind, "nonce": nonce})
                await self.assert_error(reader, "BAD_FIELD")
        self.assertEqual(player.conn.error_count, 6)
        await self.send(writer, {"type": "PING", "nonce": "still-connected"})
        self.assertEqual((await self.receive(reader))["nonce"], "still-connected")

    async def test_chat_reaches_sender_and_all_connected_statuses_with_real_sender(self):
        peers = await self.group(4)
        self.server.phase = "PLAY"
        for (_, _, player), status in zip(peers, ["ALIVE", "GUESSED", "FORFEIT", "DQ"]):
            player.status = status
        r1, w1, p1 = peers[0]
        await self.send(w1, {"type": "CHAT", "text": "大家好", "from": "p999", "future": 123})
        expected = {"type": "CHAT", "from": p1.pid, "text": "大家好"}
        for reader, _, player in peers:
            self.assertEqual(await self.receive(reader), expected)
            self.assertEqual(player.records, [])
            self.assertEqual(len(player.cands), 5040)
        self.assertEqual(self.server.phase, "PLAY")

    async def test_chat_does_not_reach_removed_players(self):
        (r1, w1, p1), (r2, w2, p2) = await self.group()
        self.server.phase = "COMMIT"
        p2.status = "REMOVED"
        await self.send(w1, {"type": "CHAT", "text": "hello"})
        self.assertEqual((await self.receive(r1))["type"], "CHAT")
        await self.send(w2, {"type": "PING", "nonce": "no-chat-before-this"})
        self.assertEqual(await self.receive(r2), {"type": "PONG", "nonce": "no-chat-before-this"})

    async def test_chat_validation_is_private_and_correctable(self):
        (r1, w1, p1), (r2, w2, p2) = await self.group()
        invalid = [(None, "BAD_FIELD"), (123, "BAD_FIELD"), ("", "INVALID_VALUE"),
                   ("中" * 201, "INVALID_VALUE"), ("a\n", "INVALID_VALUE"),
                   ("a\u200b", "INVALID_VALUE"), ("a\ue000", "INVALID_VALUE")]
        for text, code in invalid:
            await self.send(w1, {"type": "CHAT", "text": text})
            await self.assert_error(r1, code)
        self.assertEqual(p1.conn.error_count, len(invalid))
        await self.send(w2, {"type": "PING", "nonce": "private-errors"})
        self.assertEqual((await self.receive(r2))["type"], "PONG")
        await self.send(w1, {"type": "CHAT", "text": "中" * 200})
        for reader in [r1, r2]:
            self.assertEqual((await self.receive(reader))["text"], "中" * 200)
        self.assertEqual(p1.conn.error_count, len(invalid))
        self.assertEqual(p2.conn.error_count, 0)

    async def test_chat_and_ping_allowed_in_every_active_phase(self):
        reader, writer, player = await self.joined()
        for phase in ["LOBBY", "COMMIT", "PLAY", "END"]:
            self.server.phase = phase
            await self.send(writer, {"type": "CHAT", "text": phase})
            self.assertEqual((await self.receive(reader))["text"], phase)
            await self.send(writer, {"type": "PING", "nonce": phase})
            self.assertEqual(await self.receive(reader), {"type": "PONG", "nonce": phase})
        self.assertEqual(player.conn.error_count, 0)

    async def test_a12_messages_before_hello_are_fatal_unexpected(self):
        for msg in [{"type": "CHAT", "text": "hello"}, {"type": "PING"},
                    {"type": "PONG"}, {"type": "BYE"}]:
            reader, writer = await self.client()
            await self.send(writer, msg)
            await self.assert_error(reader, "UNEXPECTED", True)
        self.assertEqual(self.server.players, {})
        self.assertEqual(self.server.next_id, 1)

    async def test_bye_removes_lobby_player_and_broadcasts_without_ack(self):
        (r1, w1, p1), (r2, _, p2) = await self.group()
        await self.send(w1, {"type": "BYE"})
        await self.eof(r1)
        lobby = await self.receive(r2)
        self.assertEqual(lobby["type"], "LOBBY")
        self.assertEqual([p["id"] for p in lobby["players"]], [p2.pid])
        self.assertNotIn(p1.pid, self.server.players)
        await self.changed_until(lambda: len(self.server.conns) == 1)

    async def test_bye_during_play_preserves_state_and_discards_following_guess(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.guesser = player.pid
        self.server.guess_open = True
        self.server.reveal_wait.add(player.pid)
        player.status = "ALIVE"
        writer.write(encode({"type": "BYE"})
                     + encode({"type": "GUESS", "turn_id": 3, "guess": "0275"}))
        await writer.drain()
        await self.eof(reader)
        self.assertIn(player.pid, self.server.players)
        self.assertEqual(player.status, "ALIVE")
        self.assertIsNone(player.out_turn)
        self.assertIsNone(self.server.cur_guess)
        self.assertIn(player.pid, self.server.reveal_wait)
        self.assertFalse(any(row["msg"].get("type") == "GUESS" for row in self.logs()))

    async def test_twentieth_error_keeps_connection_and_twenty_first_sends_two_then_eof(self):
        reader, writer, player = await self.joined()
        for _ in range(20):
            await self.send(writer, {"type": "FUTURE"})
            await self.assert_error(reader, "UNKNOWN_TYPE")
        self.assertEqual(player.conn.error_count, 20)
        self.assertFalse(player.conn.closed)
        await self.send(writer, {"type": "PING", "nonce": "count-must-stay-20"})
        self.assertEqual((await self.receive(reader))["type"], "PONG")
        self.assertEqual(player.conn.error_count, 20)
        writer.write(encode({"type": "FUTURE"}) + encode({"type": "PING"}))
        await writer.drain()
        await self.assert_error(reader, "UNKNOWN_TYPE")
        await self.assert_error(reader, "TOO_MANY_ERRORS", True)
        self.assertEqual(player.conn.error_count, 21)
        errors = [row["msg"] for row in self.logs()
                  if row["dir"] == "send" and row["msg"].get("type") == "ERROR"]
        self.assertEqual(len(errors), 22)
        self.assertEqual(sum(msg["code"] == "TOO_MANY_ERRORS" for msg in errors), 1)

    async def test_error_budget_carries_from_before_hello_to_after_hello(self):
        reader, writer = await self.client()
        writer.write(b"bad json\n" * 20 + encode(hello("one", "G09", "ai")))
        await writer.drain()
        for _ in range(20):
            await self.assert_error(reader, "BAD_JSON")
        welcome = await self.receive(reader)
        await self.receive(reader)
        player = self.server.players[welcome["player_id"]]
        self.assertEqual(player.conn.error_count, 20)
        await self.send(writer, {"type": "FUTURE"})
        await self.assert_error(reader, "UNKNOWN_TYPE")
        await self.assert_error(reader, "TOO_MANY_ERRORS", True)

    async def test_bad_json_twenty_one_times_before_hello_closes_before_later_hello(self):
        reader, writer = await self.client()
        writer.write(b"bad json\n" * 21 + encode(hello("ignored", "G09", "ai")))
        await writer.drain()
        for _ in range(21):
            await self.assert_error(reader, "BAD_JSON")
        await self.assert_error(reader, "TOO_MANY_ERRORS", True)
        self.assertEqual(self.server.next_id, 1)
        self.assertEqual(self.server.players, {})

    async def test_fatal_twenty_first_error_is_followed_by_too_many_errors(self):
        reader, writer = await self.client()
        invalid_hello = {**hello("one", "G09", "ai"), "protocol": "BC/2.0"}
        writer.write(b"bad json\n" * 20 + encode(invalid_hello))
        await writer.drain()
        for _ in range(20):
            await self.assert_error(reader, "BAD_JSON")
        # 這裡不能用會立即讀 EOF 的 assert_error(..., True)。
        first = await self.receive(reader)
        second = await self.receive(reader)
        self.assertEqual([first["code"], second["code"]], ["VERSION_UNSUPPORTED", "TOO_MANY_ERRORS"])
        self.assertEqual([first["fatal"], second["fatal"]], [True, True])
        await self.eof(reader)

    async def test_different_errors_accumulate_once_per_rejected_message(self):
        reader, writer, player = await self.joined()
        cases = [
            (b"bad json\n", "BAD_JSON"),
            (encode({"type": "GUESS", "turn_id": True, "guess": "0275"}), "BAD_FIELD"),
            (encode({"type": "GUESS", "turn_id": 3, "guess": "1123"}), "UNEXPECTED"),
            (encode({"type": "CHAT", "text": ""}), "INVALID_VALUE"),
            (encode({"type": "FUTURE"}), "UNKNOWN_TYPE"),
            (encode({"type": "HELLO"}), "UNEXPECTED"),
        ]
        for count, (raw, code) in enumerate(cases, 1):
            writer.write(raw)
            await writer.drain()
            await self.assert_error(reader, code)
            self.assertEqual(player.conn.error_count, count)

    async def test_error_budget_is_per_connection_and_does_not_change_game_state(self):
        (r1, w1, p1), (r2, w2, p2) = await self.group()
        self.server.phase = "PLAY"
        p1.status = p2.status = "ALIVE"
        writer_data = encode({"type": "FUTURE"}) * 21
        w1.write(writer_data)
        await w1.drain()
        for _ in range(21):
            await self.assert_error(r1, "UNKNOWN_TYPE")
        await self.assert_error(r1, "TOO_MANY_ERRORS", True)
        self.assertEqual(p1.conn.error_count, 21)
        self.assertEqual(p1.status, "ALIVE")
        self.assertIn(p1.pid, self.server.players)
        await self.send(w2, {"type": "PING", "nonce": "healthy"})
        self.assertEqual(await self.receive(r2), {"type": "PONG", "nonce": "healthy"})
        self.assertEqual(p2.conn.error_count, 0)
        self.assertFalse(p2.conn.closed)

    async def test_client_timestamps_cannot_reopen_closed_guess_window(self):
        reader, writer, player = await self.joined()
        self.server.phase = "PLAY"
        self.server.turn_id = 3
        self.server.guesser = player.pid
        self.server.guess_open = False
        await self.send(writer, {"type": "GUESS", "turn_id": 3, "guess": "0275",
                                 "timestamp": 0, "deadline": 999999999999, "guess_timeout": 999999})
        await self.assert_error(reader, "UNEXPECTED")
        self.assertIsNone(self.server.cur_guess)
        self.assertFalse(self.server.guess_open)
        await self.send(writer, {"type": "PING", "nonce": "ignored-extra", "deadline": "not-trusted"})
        self.assertEqual((await self.receive(reader))["type"], "PONG")
        self.assertEqual(player.conn.error_count, 1)

    async def test_python_expression_is_bad_json_and_connection_can_recover(self):
        reader, writer = await self.client()
        writer.write(b"{'type': 'PING'}\n" + encode(hello("one", "G09", "ai")))
        await writer.drain()
        await self.assert_error(reader, "BAD_JSON")
        welcome = await self.receive(reader)
        await self.receive(reader)
        self.assertEqual(self.server.players[welcome["player_id"]].conn.error_count, 1)

    async def test_over_phase_does_not_process_a12_messages_or_count_errors(self):
        reader, writer, player = await self.joined()
        self.server.phase = "OVER"
        writer.write(encode({"type": "CHAT", "text": "ignored"})
                     + encode({"type": "PING"}) + encode({"type": "BYE"}))
        await writer.drain()
        await self.eof(reader)
        self.assertEqual(player.conn.error_count, 0)
        self.assertEqual([row["msg"]["type"] for row in self.logs() if row["dir"] == "send"], ["WELCOME", "LOBBY"])


if __name__ == "__main__":
    unittest.main()
