"""S8：§5.2 參數、根目錄入口與三身分分派。"""

import asyncio
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bc


class ParserTests(unittest.TestCase):
    def parse_error(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            bc.parse_args(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_server_defaults_match_spec(self):
        self.assertEqual(vars(bc.parse_args(["server"])), {
            "command": "server", "host": "0.0.0.0", "port": 5040,
            "players": 2, "bots": 0, "guess_timeout": 60, "feedback_timeout": 60,
            "commit_timeout": 30, "reveal_timeout": 15, "max_turns": 120,
            "max_skips": 3, "games": 1, "log_dir": "logs",
        })

    def test_server_accepts_every_required_option_and_fractional_timeouts(self):
        args = bc.parse_args([
            "server", "--host", "127.0.0.1", "--port", "5600", "--players", "6",
            "--bots", "2", "--guess-timeout", "1.5", "--feedback-timeout", "0.25",
            "--commit-timeout", "2.5", "--reveal-timeout", "3.5",
            "--max-turns", "40", "--max-skips", "2", "--games", "0", "--log-dir", "test-logs",
        ])
        self.assertEqual((args.host, args.port, args.players, args.bots), ("127.0.0.1", 5600, 6, 2))
        self.assertEqual((args.guess_timeout, args.feedback_timeout, args.commit_timeout, args.reveal_timeout),
                         (1.5, 0.25, 2.5, 3.5))
        self.assertEqual((args.max_turns, args.max_skips, args.games, args.log_dir), (40, 2, 0, "test-logs"))

    def test_participant_defaults_and_ai_quiet(self):
        for command in ["client", "ai"]:
            args = bc.parse_args([command, "--host", "localhost", "--name", "G09-阿明"])
            self.assertEqual((args.host, args.port, args.name, args.team), ("localhost", 5040, "G09-阿明", "G00"))
        args = bc.parse_args(["ai", "--host", "localhost", "--name", "bot", "--team", "G09", "--quiet"])
        self.assertTrue(args.quiet)
        self.assertEqual(args.team, "G09")
        self.parse_error(["client", "--host", "localhost", "--name", "human", "--quiet"])

    def test_subcommand_host_and_name_are_required(self):
        self.parse_error([])
        for command in ["client", "ai"]:
            self.parse_error([command])
            self.parse_error([command, "--host", "localhost"])
            self.parse_error([command, "--name", "player"])

    def test_player_bot_and_port_ranges(self):
        for options in [
            ["--players", "1"], ["--players", "7"], ["--players", "2.5"],
            ["--bots", "-1"], ["--players", "2", "--bots", "3"],
            ["--port", "0"], ["--port", "65536"],
            ["--max-turns", "0"], ["--max-skips", "0"], ["--games", "-1"],
        ]:
            with self.subTest(options=options):
                self.parse_error(["server", *options])

    def test_all_timeout_options_reject_nonpositive_and_nonfinite_values(self):
        for option in ["guess", "feedback", "commit", "reveal"]:
            for value in ["0", "-1", "nan", "inf", "1e999", "not-a-number"]:
                with self.subTest(option=option, value=value):
                    self.parse_error(["server", f"--{option}-timeout={value}"])

    def test_name_and_team_format(self):
        for option in ["--name", "--team"]:
            for value in ["", "a" * 21, "a\n", "a\u200b"]:
                self.parse_error(["ai", "--host", "localhost", "--name", "valid", option, value])

    def test_root_and_three_subcommand_help_work_without_starting_network(self):
        for args in [["--help"], ["server", "--help"], ["client", "--help"], ["ai", "--help"]]:
            result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "bc.py"), *args],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)

    def test_entry_point_is_import_safe(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", "import bc"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_dispatch_passes_namespace_and_awaits_serve(self):
        args = bc.parse_args(["server", "--port", "5600"])
        serve = AsyncMock()
        with patch("bcproto.server.Server", return_value=SimpleNamespace(serve=serve)) as factory:
            await bc.run(args)
        factory.assert_called_once_with(args)
        serve.assert_awaited_once_with()

    async def test_client_dispatch_awaits_session_run(self):
        args = bc.parse_args(["client", "--host", "localhost", "--name", "human"])
        with patch("bcproto.client.session.run", new_callable=AsyncMock, create=True) as run:
            await bc.run(args)
        run.assert_awaited_once_with(args)

    async def test_ai_dispatch_passes_all_player_options(self):
        args = bc.parse_args(["ai", "--host", "localhost", "--port", "5601", "--name", "bot",
                              "--team", "G09", "--quiet"])
        run = AsyncMock()
        with patch("bcproto.ai.agent.AIAgent", return_value=SimpleNamespace(run=run)) as factory:
            await bc.run(args)
        factory.assert_called_once_with(host="localhost", port=5601, name="bot", team="G09", quiet=True)
        run.assert_awaited_once_with()

    async def test_missing_teammate_interfaces_report_explicit_dependency(self):
        with patch("bcproto.server.Server", return_value=SimpleNamespace()):
            with self.assertRaisesRegex(bc.EntryPointUnavailable, "B18"):
                await bc.run(bc.parse_args(["server"]))
        with patch("bcproto.client.session.run", None, create=True):
            with self.assertRaisesRegex(bc.EntryPointUnavailable, "session.run"):
                await bc.run(bc.parse_args(["client", "--host", "localhost", "--name", "human"]))


class MainTests(unittest.TestCase):
    def test_main_runs_selected_entry(self):
        with patch("bc.run", new_callable=AsyncMock) as run:
            self.assertEqual(bc.main(["server"]), 0)
        self.assertEqual(run.await_args.args[0].command, "server")

    def test_main_reports_unavailable_component_without_traceback(self):
        stderr = io.StringIO()
        with patch("bc.run", new_callable=AsyncMock,
                   side_effect=bc.EntryPointUnavailable("B18 missing")), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                bc.main(["server"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("B18 missing", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
