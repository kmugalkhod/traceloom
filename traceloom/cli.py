"""Command line entry points: serve, migrate, worker, seed-demo."""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence

from traceloom.config import Settings, get_settings


def run_migrate(settings: Settings | None = None) -> int:
    from traceloom.db.session import make_engine, upgrade_to_head

    engine = make_engine((settings or get_settings()).database_url)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    print("database is at head")
    return 0


def run_seed_demo(settings: Settings | None = None) -> int:
    from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
    from traceloom.services.demo import seed_demo

    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    try:
        upgrade_to_head(engine)  # a fresh database works without a separate `migrate`
        info = seed_demo(settings, make_session_factory(engine))
    finally:
        engine.dispose()
    print(f"demo {'created' if info.created else 'already present'}")
    print(f"email:        {info.email}")
    print(f"password:     {info.password}")
    print(f"org id:       {info.org_id}")
    print(f"project id:   {info.project_id}")
    print(f"public key:   {info.public_key}")
    print(f"secret key:   {info.secret_key}")
    print()
    print("TRACELOOM_HOST=http://127.0.0.1:8000 \\")
    print(f"TRACELOOM_PUBLIC_KEY={info.public_key} \\")
    print(f"TRACELOOM_SECRET_KEY={info.secret_key} \\")
    print("python examples/agent.py")
    return 0


def run_worker(settings: Settings | None = None, stop: threading.Event | None = None) -> int:
    """Run the ingestion consumer and sweeper (started by the container's startup hooks) until
    SIGINT/SIGTERM or `stop` is set, then shut down."""
    from traceloom.container import build_container

    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    container = build_container(settings or get_settings())
    container.startup()
    print("worker started", flush=True)
    try:
        stop.wait()
    finally:
        container.shutdown()
    print("worker stopped", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceloom")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("worker", help="run the ingestion worker (consumer and sweeper)")
    sub.add_parser("seed-demo", help="create a demo user, org, project and key")
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("traceloom.main:app", host=args.host, port=args.port)
        return 0
    return {"migrate": run_migrate, "worker": run_worker, "seed-demo": run_seed_demo}[
        args.command
    ]()


if __name__ == "__main__":
    raise SystemExit(main())
