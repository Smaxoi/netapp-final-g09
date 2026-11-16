"""A8：FIFO、致命錯誤、慢速連線隔離與取消／關閉生命週期測試。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bcproto.server.net import Conn
from bcproto.server.checks import check_message
from bcproto.server import Player, Server
from bcproto.wire import decode_line, encode


class FakeWriter:
    """以 Event 控制 drain，確定重現背壓，不依賴隨機的網路速度。"""

    def __init__(self, *, blocked=False, write_error=None, drain_error=None,
                 close_error=None, block_close=False):
        self.writes = []
        self.events = []
        self.transport = self
        self.aborted = False
        self.closing = False
        self.write_error = write_error
        self.drain_error = drain_error
        self.close_error = close_error
        self.block_close = block_close
        self.drain_entered = asyncio.Event()
        self.allow_drain = asyncio.Event()
        self.allow_close = asyncio.Event()
        if not blocked:
            self.allow_drain.set()

    def write(self, data):
        if self.write_error:
            raise self.write_error
        self.events.append("write")
        self.writes.append(data)

    async def drain(self):
        self.drain_entered.set()
        await self.allow_drain.wait()
        if self.drain_error:
            raise self.drain_error
        self.events.append("drain")

    def close(self):
        self.events.append("close")
        self.closing = True

    def is_closing(self):
        return self.closing

    def abort(self):
        self.aborted = True
        self.closing = True

    async def wait_closed(self):
        if self.block_close:
            await self.allow_close.wait()
        if self.close_error:
            raise self.close_error


class ConnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []

    def connection(self, writer=None, **options):
        writer = writer or FakeWriter()
        conn = Conn(writer, **options)
        self.connections.append(conn)
        return conn

    async def finish(self, conn):
        await asyncio.wait_for(conn.wait_closed(), 1)
        await conn.writer_task

    async def asyncTearDown(self):
        for conn in self.connections:
            if not conn.closed:
                conn.abort()
        await asyncio.wait_for(asyncio.gather(*(self.finish(conn) for conn in self.connections)), 2)

    def messages(self, conn):
        return [decode_line(data)[0] for data in conn.writer.writes]

    async def test_send_is_synchronous_and_preserves_fifo(self):
        conn = self.connection()
        for n in range(20):
            self.assertIsNone(conn.send({"type": "PING", "nonce": str(n)}))
        self.assertEqual(conn.writer.writes, [])
        conn.close()
        await self.finish(conn)
        self.assertEqual([msg["nonce"] for msg in self.messages(conn)], [str(n) for n in range(20)])
        self.assertIsNone(conn.failure)

    async def test_encoding_takes_snapshot_at_send_time(self):
        conn = self.connection()
        msg = {"type": "TURN", "turn_id": 1, "guesser": "p1", "alive": ["p1", "p2"]}
        conn.send(msg)
        msg["turn_id"] = 2
        msg["alive"].clear()
        conn.close()
        await self.finish(conn)
        self.assertEqual(self.messages(conn)[0]["turn_id"], 1)
        self.assertEqual(self.messages(conn)[0]["alive"], ["p1", "p2"])

    async def test_fatal_error_is_written_after_prior_messages_before_close(self):
        conn = self.connection()
        conn.send({"type": "PING"})
        conn.error("TIMEOUT", "HELLO timeout")
        self.assertFalse(conn.connected)
        conn.send({"type": "PONG"})
        await self.finish(conn)
        self.assertEqual([msg["type"] for msg in self.messages(conn)], ["PING", "ERROR"])
        self.assertTrue(self.messages(conn)[1]["fatal"])
        self.assertEqual(conn.writer.events, ["write", "drain", "write", "drain", "close"])

    async def test_nonfatal_error_keeps_connection_usable(self):
        conn = self.connection()
        conn.error("BAD_JSON")
        self.assertTrue(conn.connected)
        conn.send({"type": "PING"})
        conn.close()
        await self.finish(conn)
        self.assertFalse(self.messages(conn)[0]["fatal"])
        self.assertEqual(self.messages(conn)[1]["type"], "PING")

    async def test_hello_context_and_completed_handshake_fatal(self):
        before = self.connection()
        before.error("BAD_FIELD", message_type="HELLO")
        await self.finish(before)
        self.assertTrue(self.messages(before)[0]["fatal"])
        after = self.connection()
        after.player = SimpleNamespace(pid="p1")
        after.error("UNEXPECTED", message_type="HELLO")
        self.assertTrue(after.connected)
        after.close()
        await self.finish(after)
        self.assertFalse(self.messages(after)[0]["fatal"])

    async def test_explicit_fatal_argument_matches_contract(self):
        conn = self.connection()
        conn.error("BAD_FIELD", "bad HELLO", True)
        await self.finish(conn)
        self.assertTrue(self.messages(conn)[0]["fatal"])

    async def test_error_twenty_one_can_queue_both_errors_in_order(self):
        conn = self.connection()
        conn.player = SimpleNamespace(pid="p1")
        for _ in range(20):
            conn.error("INVALID_VALUE", "invalid guess", False)
        self.assertTrue(conn.connected)
        self.assertEqual(conn.error_count, 20)
        conn.send({"type": "PING"})  # 合法訊息不重設錯誤計數。
        conn.error("INVALID_VALUE", "invalid guess", False)
        await self.finish(conn)
        messages = self.messages(conn)
        self.assertEqual([msg["code"] for msg in messages[-2:]], ["INVALID_VALUE", "TOO_MANY_ERRORS"])
        self.assertEqual(len([msg for msg in messages if msg["type"] == "ERROR"]), 22)
        self.assertEqual(conn.error_count, 21)
        conn.error("BAD_JSON")
        self.assertEqual(conn.error_count, 21)

    async def test_fatal_twenty_first_error_also_gets_too_many_errors(self):
        conn = self.connection()
        for _ in range(20):
            conn.error("BAD_JSON")
        conn.error("BAD_FIELD", message_type="HELLO")
        await self.finish(conn)
        last_two = self.messages(conn)[-2:]
        self.assertEqual([msg["code"] for msg in last_two], ["BAD_FIELD", "TOO_MANY_ERRORS"])
        self.assertEqual([msg["fatal"] for msg in last_two], [True, True])
        self.assertEqual(conn.error_count, 21)

    async def test_invalid_error_arguments_do_not_consume_error_budget(self):
        conn = self.connection()
        for code, options, exception in [
            ("TYPO", {}, ValueError),
            ("BAD_JSON", {"fatal": 1}, TypeError),
            ("BAD_JSON", {"message": 1}, TypeError),
        ]:
            with self.assertRaises(exception):
                conn.error(code, **options)
            self.assertEqual(conn.error_count, 0)
        conn.close()
        await self.finish(conn)

    async def test_close_idle_is_idempotent_and_stops_new_messages(self):
        seen = []
        conn = self.connection(on_close=seen.append)
        conn.close()
        conn.close()
        conn.send({"type": "PING"})
        conn.error("BAD_JSON")
        await self.finish(conn)
        self.assertTrue(conn.closed)
        self.assertFalse(conn.connected)
        self.assertEqual(seen, [conn])
        self.assertEqual(conn.writer.writes, [])

    async def test_close_flushes_full_queue_without_sentinel_slot(self):
        conn = self.connection(max_pending=2)
        conn.send({"type": "PING", "nonce": "first"})
        conn.send({"type": "PING", "nonce": "last"})
        conn.close()
        await self.finish(conn)
        self.assertEqual([msg["nonce"] for msg in self.messages(conn)], ["first", "last"])

    async def test_queue_overflow_before_writer_starts_does_not_leak_task(self):
        conn = self.connection(max_pending=1)
        conn.send({"type": "PING"})
        conn.send({"type": "PONG"})
        self.assertFalse(conn.connected)
        await self.finish(conn)
        self.assertIsInstance(conn.failure, BufferError)
        self.assertTrue(conn.writer.aborted)
        self.assertEqual(conn.writer.writes, [])

    async def test_slow_client_does_not_block_other_connections(self):
        seen = []
        slow = self.connection(FakeWriter(blocked=True), max_pending=1, on_close=seen.append)
        fast = self.connection()
        slow.send({"type": "PING", "nonce": "in flight"})
        await asyncio.wait_for(slow.writer.drain_entered.wait(), 1)
        for n in range(3):
            msg = {"type": "PING", "nonce": str(n)}
            slow.send(msg)
            fast.send(msg)
        fast.close()
        await self.finish(fast)
        await self.finish(slow)
        self.assertEqual([msg["nonce"] for msg in self.messages(fast)], ["0", "1", "2"])
        self.assertIsInstance(slow.failure, BufferError)
        self.assertIsNone(fast.failure)
        self.assertEqual(seen, [slow])

    async def test_lobby_overflow_cannot_deliver_old_roster_after_new_roster(self):
        server = Server(SimpleNamespace(players=4))
        slow = self.connection(max_pending=1, srv=server, on_close=server._on_disconnect)
        fast = self.connection(srv=server, on_close=server._on_disconnect)
        for pid, conn in [("p1", slow), ("p2", fast)]:
            player = Player(pid, pid, "G09", "human", conn)
            conn.player = player
            server.players[pid] = player
        slow.send({"type": "PING"})  # 尚未啟動 writer，先填滿慢速連線佇列。
        server.broadcast_lobby()
        await asyncio.sleep(0)  # 執行延後的大廳名單更新。
        self.assertEqual(set(server.players), {"p2"})
        fast.close()
        await self.finish(slow)
        await self.finish(fast)
        rosters = [msg["players"] for msg in self.messages(fast) if msg["type"] == "LOBBY"]
        self.assertEqual([p["id"] for p in rosters[0]], ["p1", "p2"])
        self.assertEqual([p["id"] for p in rosters[-1]], ["p2"])

    async def test_write_and_drain_failures_are_isolated(self):
        for writer in [FakeWriter(write_error=ConnectionResetError("peer reset")),
                       FakeWriter(drain_error=RuntimeError("broken transport"))]:
            broken = self.connection(writer)
            healthy = self.connection()
            broken.send({"type": "PING"})
            healthy.send({"type": "PING"})
            healthy.close()
            await self.finish(broken)
            await self.finish(healthy)
            self.assertIsNotNone(broken.failure)
            self.assertTrue(broken.writer.aborted)
            self.assertIsNone(healthy.failure)
            self.assertEqual(len(healthy.writer.writes), 1)

    async def test_write_timeout_disconnects_and_notifies(self):
        seen = []
        conn = self.connection(FakeWriter(blocked=True), write_timeout=0.02, on_close=seen.append)
        conn.send({"type": "PING"})
        await self.finish(conn)
        self.assertIsInstance(conn.failure, TimeoutError)
        self.assertFalse(conn.connected)
        self.assertTrue(conn.writer.aborted)
        self.assertEqual(seen, [conn])

    async def test_close_wait_is_bounded(self):
        conn = self.connection(FakeWriter(block_close=True), close_timeout=0.02)
        conn.close()
        await self.finish(conn)
        self.assertIsInstance(conn.failure, TimeoutError)
        self.assertTrue(conn.writer.aborted)

    async def test_wait_closed_exception_is_contained(self):
        conn = self.connection(FakeWriter(close_error=OSError("close failed")))
        conn.close()
        await self.finish(conn)
        self.assertIsInstance(conn.failure, OSError)
        self.assertTrue(conn.writer.aborted)

    async def test_abort_during_drain_discards_remaining_messages(self):
        conn = self.connection(FakeWriter(blocked=True))
        conn.send({"type": "PING", "nonce": "first"})
        await asyncio.wait_for(conn.writer.drain_entered.wait(), 1)
        conn.send({"type": "PING", "nonce": "discard"})
        conn.abort()
        await self.finish(conn)
        self.assertEqual([msg["nonce"] for msg in self.messages(conn)], ["first"])
        self.assertTrue(conn.writer.aborted)

    async def test_cancelling_active_writer_still_closes_connection(self):
        conn = self.connection(FakeWriter(blocked=True))
        conn.send({"type": "PING"})
        await asyncio.wait_for(conn.writer.drain_entered.wait(), 1)
        conn.writer_task.cancel()
        await self.finish(conn)
        self.assertTrue(conn.closed)
        self.assertTrue(conn.writer.is_closing())

    async def test_cancelling_waiter_does_not_cancel_writer(self):
        conn = self.connection(FakeWriter(blocked=True))
        conn.send({"type": "PING"})
        await asyncio.wait_for(conn.writer.drain_entered.wait(), 1)
        waiter = asyncio.create_task(conn.wait_closed())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(conn.writer_task.done())
        conn.writer.allow_drain.set()
        conn.close()
        await self.finish(conn)
        self.assertIsNone(conn.failure)

    async def test_callback_failure_cannot_prevent_cleanup(self):
        def bad_callback(conn):
            raise RuntimeError("callback failed")
        conn = self.connection(on_close=bad_callback)
        conn.close()
        await self.finish(conn)
        self.assertIsInstance(conn.failure, RuntimeError)

    async def test_invalid_local_message_does_not_poison_queue(self):
        conn = self.connection()
        with self.assertRaises(ValueError):
            conn.send({"type": "PING", "extra": float("nan")})
        self.assertTrue(conn.connected)
        conn.send({"type": "PING"})
        conn.close()
        await self.finish(conn)
        self.assertEqual(self.messages(conn), [{"type": "PING"}])

    async def test_configuration_is_validated_before_task_creation(self):
        for options in [{"max_pending": 0}, {"max_pending": True}, {"max_pending": 1.5},
                        {"write_timeout": 0}, {"write_timeout": float("inf")},
                        {"close_timeout": True}, {"close_timeout": -1}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                Conn(FakeWriter(), **options)

    async def test_loopback_errors_arrive_in_order_before_eof(self):
        """使用真正的 StreamWriter，驗證 A2 → A7 → A8 → TCP EOF。"""
        accepted = asyncio.get_running_loop().create_future()

        def accept(reader, writer):
            accepted.set_result((reader, writer))

        listener = await asyncio.start_server(accept, "127.0.0.1", 0)
        client_writer = None
        try:
            port = listener.sockets[0].getsockname()[1]
            client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
            server_reader, server_writer = await asyncio.wait_for(accepted, 1)
            conn = self.connection(server_writer)
            hello = {"type": "HELLO", "protocol": "BC/2.0", "name": "", "team": "G09", "kind": "ai"}
            client_writer.write(b"not json\n" + encode(hello))
            await client_writer.drain()

            _, code = decode_line(await asyncio.wait_for(server_reader.readline(), 1))
            conn.error(code)
            self.assertTrue(conn.connected)
            msg, error = decode_line(await asyncio.wait_for(server_reader.readline(), 1))
            self.assertIsNone(error)
            state = SimpleNamespace(phase="LOBBY", players={}, a=SimpleNamespace(players=2))
            code = check_message(msg, state)
            self.assertEqual(code, "VERSION_UNSUPPORTED")
            conn.error(code, message_type=msg["type"])

            received = []
            for _ in range(2):
                raw = await asyncio.wait_for(client_reader.readline(), 1)
                decoded, error = decode_line(raw)
                self.assertIsNone(error)
                received.append(decoded)
            self.assertEqual([msg["code"] for msg in received], ["BAD_JSON", "VERSION_UNSUPPORTED"])
            self.assertEqual([msg["fatal"] for msg in received], [False, True])
            self.assertEqual(await asyncio.wait_for(client_reader.read(), 1), b"")
            await self.finish(conn)
            self.assertIsNone(conn.failure)
        finally:
            if client_writer is not None:
                client_writer.close()
                await client_writer.wait_closed()
            listener.close()
            await listener.wait_closed()


if __name__ == "__main__":
    unittest.main()
