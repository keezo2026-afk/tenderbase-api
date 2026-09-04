"""Windows event-loop compatibility.

Python 3.8+ makes :class:`~asyncio.ProactorEventLoop` the default on Windows.
psycopg's async implementation cannot run on it and raises::

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in
    async mode.

Every async entry point in this project talks to PostgreSQL through psycopg, so
on Windows the selector policy has to be installed *before* any loop is created.
This module is imported by :mod:`app` itself, which means importing anything
from the application — a script, the ASGI app, Alembic's ``env.py`` — is enough
to get a working loop. There is deliberately no opt-in step for the caller to
forget.

On non-Windows platforms this is a no-op: the default selector loop is already
compatible, and ``WindowsSelectorEventLoopPolicy`` does not exist there.
"""

from __future__ import annotations

import asyncio
import sys


def install_windows_event_loop_policy() -> bool:
    """Install the selector event-loop policy when running on Windows.

    Returns ``True`` if the policy was changed, ``False`` on platforms that do
    not need it. Safe to call repeatedly.
    """
    if sys.platform != "win32":
        return False

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover - defensive, Windows-only path
        return False

    if isinstance(asyncio.get_event_loop_policy(), policy_cls):
        return False

    asyncio.set_event_loop_policy(policy_cls())
    return True
