"""Who the app is working for, when more than one person uses it.

The Mac mini's owner reaches the app over Tailscale (or on the Mac itself) and
keeps the library they always had. Guests sign in with Google through
Cloudflare Access (see :mod:`stompxl.access`); each gets their own library,
settings, backups and relayed pedal under ``<data dir>/users/<email>/``, and
never sees anyone else's.

The current person is a context variable, set once per request by the access
gate. FastAPI carries it into the worker threads that run sync endpoints, and
:func:`spawn` carries it into the app's own background jobs. It is the same
variable the pedal relay keys on, so a guest's pedal work only ever reaches the
pedal that guest plugged in.

Claude runs on the owner's API key for everyone, so guests get a daily
allowance of Claude jobs (``STOMPXL_GUEST_DAILY_CLAUDE``, default 25).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from stompxl.helixgen.device.relay import SCOPE

OWNER = ""
DAILY_CLAUDE_ENV = "STOMPXL_GUEST_DAILY_CLAUDE"
DEFAULT_DAILY_CLAUDE = 25
_usage_lock = threading.Lock()


class OverAllowance(RuntimeError):
    pass


def current() -> str:
    """The signed-in guest's email, or ``""`` for the owner."""
    return SCOPE.get()


def is_guest() -> bool:
    return current() != OWNER


@contextlib.contextmanager
def acting_as(user: str) -> Iterator[None]:
    token = SCOPE.set(user)
    try:
        yield
    finally:
        SCOPE.reset(token)


def spawn(target: Callable[[], Any], *, name: str | None = None) -> threading.Thread:
    """Start a daemon thread that keeps working for whoever started it."""
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(target,), daemon=True, name=name)
    thread.start()
    return thread


def folder_name(email: str) -> str:
    """A guest's folder name: their email, with anything odd made safe."""
    cleaned = re.sub(r"[^a-z0-9@._+-]", "_", email.strip().lower())
    return cleaned.strip(".") or "_"


def guest_dir(base: Path, email: str) -> Path:
    return base / "users" / folder_name(email)


def daily_allowance() -> int:
    try:
        return max(0, int(os.environ.get(DAILY_CLAUDE_ENV, DEFAULT_DAILY_CLAUDE)))
    except ValueError:
        return DEFAULT_DAILY_CLAUDE


def _usage_path() -> Path:
    from . import config

    return config.data_dir() / "claude-usage.json"


def claude_used_today() -> int:
    try:
        usage = json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(usage.get(time.strftime("%Y-%m-%d"), 0)) if isinstance(usage, dict) else 0


def charge_claude_job() -> None:
    """Count one Claude job against a guest's daily allowance; the owner is never limited."""
    if not is_guest():
        return
    limit = daily_allowance()
    with _usage_lock:
        used = claude_used_today()
        if used >= limit:
            raise OverAllowance(
                f"You've used today's {limit} AI jobs. They reset at midnight; "
                "anything that doesn't need the AI still works.")
        today = time.strftime("%Y-%m-%d")
        path = _usage_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({today: used + 1}), encoding="utf-8")
        tmp.replace(path)
