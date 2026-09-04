"""The Windows event-loop policy must be installed on package import.

psycopg refuses to run in async mode on Windows' default ``ProactorEventLoop``,
which made every operator script and the ASGI app unusable on Windows with a
PostgreSQL DSN. The guard lives in ``app/__init__.py`` so that no entry point
has to remember it; these tests pin that contract down, including on Linux and
macOS where the code path is a no-op but the import must still be harmless.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from app.utils.event_loop import install_windows_event_loop_policy

pytestmark = pytest.mark.unit


def test_is_a_noop_off_windows() -> None:
    """On POSIX the default loop already works; nothing should be swapped."""
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        pytest.skip("POSIX-only assertion")

    before = asyncio.get_event_loop_policy()
    assert install_windows_event_loop_policy() is False
    assert asyncio.get_event_loop_policy() is before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_installs_selector_policy_on_windows() -> None:  # pragma: no cover
    """The active policy must be the selector one psycopg can drive."""
    install_windows_event_loop_policy()
    assert isinstance(
        asyncio.get_event_loop_policy(),
        asyncio.WindowsSelectorEventLoopPolicy,  # type: ignore[attr-defined]
    )


def test_calling_twice_is_safe() -> None:
    """Scripts may import app more than once; the call must be idempotent."""
    install_windows_event_loop_policy()
    first = asyncio.get_event_loop_policy()
    install_windows_event_loop_policy()
    assert asyncio.get_event_loop_policy() is first


def test_importing_app_installs_the_policy() -> None:
    """A bare ``import app`` is the only thing entry points have in common.

    Run in a subprocess: the policy is process-wide state and this assertion is
    about what a *fresh* interpreter sees, not what the test session already did.
    """
    code = (
        "import asyncio, sys; import app; "
        "expected = getattr(asyncio, 'WindowsSelectorEventLoopPolicy', None); "
        "ok = True if sys.platform != 'win32' or expected is None "
        "else isinstance(asyncio.get_event_loop_policy(), expected); "
        "print('OK' if ok else 'WRONG-POLICY')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "OK"
