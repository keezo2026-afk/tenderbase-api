"""The dev runner must hand uvicorn a psycopg-compatible loop.

``uvicorn.loops.asyncio.asyncio_loop_factory`` returns ``ProactorEventLoop`` on
Windows and builds it *after* importing the app, so the policy installed in
``app/__init__.py`` is overridden and ``--loop asyncio`` selects that same
factory. The result was an API that started cleanly and answered 503 on every
data route. ``scripts.run_api`` exists solely to construct the loop itself.
"""

from __future__ import annotations

import asyncio
import selectors
import sys

import pytest

from scripts.run_api import _loop_factory, build_parser

pytestmark = pytest.mark.unit


def test_loop_is_never_a_proactor_loop() -> None:
    """The one property that actually matters to psycopg."""
    loop = _loop_factory()
    try:
        proactor = getattr(asyncio, "ProactorEventLoop", None)
        assert proactor is None or not isinstance(loop, proactor)
    finally:
        loop.close()


def test_loop_is_usable() -> None:
    """A loop that cannot run a coroutine would fail late and confusingly."""

    async def answer() -> int:
        return 42

    loop = _loop_factory()
    try:
        assert loop.run_until_complete(answer()) == 42
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_uses_select_selector_on_windows() -> None:  # pragma: no cover
    loop = _loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        assert isinstance(loop._selector, selectors.SelectSelector)  # type: ignore[attr-defined]
    finally:
        loop.close()


def test_each_call_returns_a_fresh_loop() -> None:
    first, second = _loop_factory(), _loop_factory()
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_parser_defaults_to_localhost_8000() -> None:
    args = build_parser().parse_args([])
    assert (args.host, args.port, args.reload) == ("127.0.0.1", 8000, False)


def test_parser_accepts_overrides() -> None:
    args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "9000", "--reload"])
    assert (args.host, args.port, args.reload) == ("0.0.0.0", 9000, True)
