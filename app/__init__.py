"""TenderBase API — application package.

Importing this package installs the Windows-compatible asyncio policy (a no-op
elsewhere). psycopg cannot drive PostgreSQL on Windows' default
``ProactorEventLoop``, and every entry point — the ASGI app, the operator
scripts, Alembic — reaches the database through it, so the fix belongs at the
one import they all share rather than in each ``main()``.
"""

from app.utils.event_loop import install_windows_event_loop_policy

install_windows_event_loop_policy()

__version__ = "0.1.0"
