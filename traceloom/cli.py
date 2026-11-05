"""Command line entry points: serve, migrate, worker, seed-demo."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _not_yet(name: str) -> int:
    print(f"traceloom {name}: not available yet", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceloom")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    for name, help_ in (
        ("migrate", "apply database migrations"),
        ("worker", "run the ingestion worker (full mode)"),
        ("seed-demo", "create a demo user, org, project and key"),
    ):
        sub.add_parser(name, help=help_)
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("traceloom.main:app", host=args.host, port=args.port)
        return 0
    return _not_yet(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
