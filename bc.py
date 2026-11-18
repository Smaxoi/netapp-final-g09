"""S8：BC/1.0 命令列入口（PDF §5.2）。

只負責參數與分派，不實作 B18 的伺服器生命週期或 C 的人類客戶端。
接點：async Server.serve()、async client.session.run(args)、AIAgent.run()。
"""

import argparse
import asyncio
import math

from bcproto.validators import valid_text


class EntryPointUnavailable(RuntimeError):
    """其他模組尚未提供執行介面。"""


def _integer_at_least(minimum):
    def parse(value):
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return number
    return parse


def _port(value):
    number = _integer_at_least(1)(value)
    if number > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def _players(value):
    number = _integer_at_least(2)(value)
    if number > 6:
        raise argparse.ArgumentTypeError("players must be between 2 and 6")
    return number


def _seconds(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return number


def _text(value):
    if not valid_text(value):
        raise argparse.ArgumentTypeError("must be 1-20 Unicode characters without control characters")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BC/1.0 server, client and AI", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("server", help="host a game", allow_abbrev=False)
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=_port, default=5040)
    server.add_argument("--players", type=_players, default=2)
    server.add_argument("--bots", type=_integer_at_least(0), default=0)
    for option, default in [("guess", 60), ("feedback", 60), ("commit", 30), ("reveal", 15)]:
        server.add_argument(f"--{option}-timeout", type=_seconds, default=default)
    server.add_argument("--max-turns", type=_integer_at_least(1), default=120)
    server.add_argument("--max-skips", type=_integer_at_least(1), default=3)
    server.add_argument("--games", type=_integer_at_least(0), default=1)
    server.add_argument("--log-dir", default="logs")

    for command in ("client", "ai"):
        participant = commands.add_parser(command, help=f"join as {command}", allow_abbrev=False)
        participant.add_argument("--host", required=True)
        participant.add_argument("--port", type=_port, default=5040)
        participant.add_argument("--name", type=_text, required=True)
        participant.add_argument("--team", type=_text, default="G00")
        participant.add_argument("--client-log-dir", default="client-logs")
        if command == "ai":
            participant.add_argument("--quiet", action="store_true")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "server" and args.bots > args.players:
        parser.error("--bots must not exceed --players")
    return args


async def run(args) -> None:
    """延後匯入各身分，讓 --help 與參數檢查不受其他模組完成度影響。"""
    if args.command == "server":
        from bcproto.server import Server

        server = Server(args)
        serve = getattr(server, "serve", None)
        if not callable(serve):
            raise EntryPointUnavailable(
                "server startup is not implemented: B18 must provide async Server.serve().")
        await serve()
    elif args.command == "client":
        from bcproto.client import session

        client_run = getattr(session, "run", None)
        if not callable(client_run):
            raise EntryPointUnavailable(
                "human client startup is not implemented: C must provide async "
                "bcproto.client.session.run(args).")
        await client_run(args)
    elif args.command == "ai":
        from bcproto.ai.agent import AIAgent

        agent = AIAgent(host=args.host, port=args.port, name=args.name,
                        team=args.team, quiet=args.quiet)
        await agent.run()
    else:
        raise ValueError(f"Unknown command: {args.command}")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except (EntryPointUnavailable, ImportError) as error:
        build_parser().exit(2, f"bc.py: error: {error}\n")
    except OSError as error:
        build_parser().exit(1, f"bc.py: error: {error}\n")
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
