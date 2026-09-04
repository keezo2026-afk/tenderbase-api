"""Development server entry point that works on Windows.

Why this exists
---------------
``uvicorn.loops.asyncio.asyncio_loop_factory`` hardcodes
:class:`~asyncio.ProactorEventLoop` on ``win32``::

    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop

It builds that loop itself, *after* importing the application, so the policy
installed in :mod:`app` is overridden and ``--loop asyncio`` does not help — it
selects this very factory. psycopg then refuses to connect and the API starts
up with an unreachable database, answering ``503`` on every data route.

Passing an explicit ``loop_factory`` to :class:`uvicorn.Server` is the supported
way to override that, so this module does exactly that and nothing else. On
POSIX it is a thin wrapper around the same server uvicorn would have run.

Usage::

    python -m scripts.run_api                 # 127.0.0.1:8000
    python -m scripts.run_api --port 9000 --reload
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys

import uvicorn

import app  # noqa: F401 - installs the Windows event-loop policy on import


def _loop_factory() -> asyncio.AbstractEventLoop:
    """A psycopg-compatible loop on every platform."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TenderBase API locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    parser.add_argument("--log-level", default="info")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.reload:
        # Reload runs the app in a subprocess, and uvicorn already picks a
        # selector loop for those, so the standard runner is correct here.
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level=args.log_level,
        )
        return 0

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    server = uvicorn.Server(config)
    loop = _loop_factory()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
