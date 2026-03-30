from __future__ import annotations

import argparse

from punkrecords.proxy import run_proxy_server
from punkrecords.store import AccountRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="punkrecords", description="Run the PunkRecords local proxy server")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="Show server usage instructions")
    proxy_parser = subparsers.add_parser("proxy", help="Run the local proxy server")
    proxy_parser.add_argument("--host", default="0.0.0.0", help="Host to bind the local proxy to")
    proxy_parser.add_argument("--port", type=int, default=4141, help="Port to bind the local proxy to")
    return parser


def print_help_command() -> int:
    print("PunkRecords")
    print("Primary CLI: punkrecords")
    print("This CLI is now limited to starting the local proxy server.")
    print()
    print("Run the server:")
    print("  uv run punkrecords proxy --host 0.0.0.0 --port 4141")
    print()
    print("Once the server is running, use the HTTP API for administration and future web UI flows.")
    print("Useful commands:")
    print("- help")
    print("- proxy [--host HOST] [--port PORT]")
    return 0


def handle_proxy(args: argparse.Namespace) -> int:
    repo = AccountRepository()
    return run_proxy_server(repo, host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "help":
        return print_help_command()
    if args.command == "proxy":
        return handle_proxy(args)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
