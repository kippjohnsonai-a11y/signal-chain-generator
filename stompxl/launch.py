"""What a click on the desktop app does (``signal-chain-generator --launch``).

The Windows installer's shortcut runs this with no console window:

1. If this version is already running on the port, open it in the browser.
2. If another version is running there, stop it (its process id is in
   ``server.pid`` in the data folder).
3. Otherwise start the server, with its output in ``app.log`` in the data
   folder, and open the browser once it answers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)


def pid_path() -> Path:
    return config.base_dir() / "server.pid"


def log_path() -> Path:
    return config.base_dir() / "app.log"


def running_version(url: str, timeout: float = 1.0) -> str | None:
    """The version a server on ``url`` reports, or None when nothing answers."""
    try:
        with urllib.request.urlopen(f"{url}api/version", timeout=timeout) as response:
            return str(json.load(response).get("commit") or "")
    except (OSError, ValueError):
        return None


def this_version() -> str:
    from .server import VERSION

    return str(VERSION["commit"])


def stop_running(url: str, wait: float = 10.0) -> bool:
    """Stop the server whose process id we wrote; True once the port is free."""
    try:
        pid = int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return running_version(url) is None
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if running_version(url, timeout=0.5) is None:
            return True
        time.sleep(0.25)
    return False


def write_pid() -> None:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    with contextlib.suppress(OSError, ValueError):
        if int(pid_path().read_text(encoding="utf-8").strip()) == os.getpid():
            pid_path().unlink()


def log_to_file() -> None:
    """No console when started from a shortcut: send all output to app.log."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stream: Any = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    sys.stdout = sys.stderr = stream


def already_running(url: str, open_browser: Any) -> bool:
    """Steps 1 and 2: True when a running server of this version was opened."""
    running = running_version(url)
    if running is None:
        return False
    if running == this_version():
        open_browser(url)
        return True
    logger.info("Stopping version %s on %s to start %s", running, url, this_version())
    if not stop_running(url):
        raise SystemExit(f"Another copy of the app (version {running}) is using {url} and didn't stop.")
    return False
