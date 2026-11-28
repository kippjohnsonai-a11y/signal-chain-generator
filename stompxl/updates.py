"""Check for updates, and install one with a click in the desktop apps.

The Mac app and the Windows shortcut are launchers: a copy of uv that runs one
exact PyPI release. Each passes the path of that uv in ``SCG_UV`` (the Mac app
also passes its launcher script in ``SCG_LAUNCHER``). With it the app can:

1. Ask PyPI once a day for the newest release (``updates.json`` in the data
   folder remembers the answer, "remind me later" and "don't check").
2. On **Update now**: download the new release with that uv, write its version
   to ``app-version`` in the data folder, and start it. The new copy stops this
   one (see ``launch.already_running``), and from then on the launchers start
   the version in ``app-version`` when it is newer than their own.

Started any other way (``uvx``, a git checkout) the app can only say that an
update is out and how to get it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

PACKAGE = "signal-chain-generator"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_EVERY = 24 * 3600
UV_ENV = "SCG_UV"
LAUNCHER_ENV = "SCG_LAUNCHER"
NO_OPEN_ENV = "SCG_NO_OPEN"
_VERSION = re.compile(r"^\d+(\.\d+){1,3}$")

_lock = threading.Lock()
_install: dict[str, Any] = {"state": "idle", "error": None, "version": None}


def parse(version: str | None) -> tuple[int, ...] | None:
    """(0, 2, 2) for "0.2.2"; None for anything that isn't a plain release."""
    if not version or not _VERSION.match(version.strip()):
        return None
    return tuple(int(part) for part in version.strip().split("."))


def newer(candidate: str | None, than: str | None) -> bool:
    a, b = parse(candidate), parse(than)
    return a is not None and (b is None or a > b)


def _state_path() -> Path:
    return config.base_dir() / "updates.json"


def pinned_path() -> Path:
    """The version the launchers start, when it is newer than their own."""
    return config.base_dir() / "app-version"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(values: dict[str, Any]) -> None:
    data = {**_read(), **values}
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def current() -> str:
    from .server import VERSION

    return str(VERSION.get("commit") or "")


def uv_path() -> str | None:
    path = os.environ.get(UV_ENV)
    return path if path and Path(path).is_file() else None


def can_install() -> bool:
    """True when started by the Mac app or the Windows shortcut."""
    if parse(current()) is None or uv_path() is None:
        return False
    return sys.platform == "win32" or bool(os.environ.get(LAUNCHER_ENV))


def fetch_latest(timeout: float = 5.0) -> str | None:
    """The newest release on PyPI, or None when PyPI can't be reached."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:
            return str(json.load(response)["info"]["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest(force: bool = False, now: float | None = None) -> str | None:
    """The newest release, asking PyPI at most once a day."""
    now = time.time() if now is None else now
    data = _read()
    checked = data.get("checked_at")
    if not force and checked is not None and now - float(checked) < CHECK_EVERY:
        return data.get("latest")
    found = fetch_latest()
    if found is None:
        return data.get("latest")
    _write({"latest": found, "checked_at": now})
    return found


def status(force: bool = False, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    data = _read()
    enabled = data.get("enabled", True) is not False
    mine = current()
    newest = latest(force=force, now=now) if (enabled or force) and parse(mine) else None
    available = newer(newest, mine)
    snoozed = available and data.get("snoozed_version") == newest and now < float(data.get("snoozed_until") or 0)
    return {
        "current": mine,
        "latest": newest,
        "available": available,
        "show": available and not snoozed,
        "enabled": enabled,
        "can_install": can_install(),
        "how": None if can_install() else how_to_update(),
        "install": dict(_install),
    }


def how_to_update() -> str:
    return ("Download the new installer from the Releases page on GitHub, or run "
            f"uvx {PACKAGE}@latest if you start the app from a terminal.")


def set_enabled(enabled: bool) -> None:
    _write({"enabled": bool(enabled)})


def snooze(days: float = 7, now: float | None = None) -> None:
    """Hide the reminder for this release for a while ("remind me later")."""
    now = time.time() if now is None else now
    _write({"snoozed_version": _read().get("latest"), "snoozed_until": now + days * 86400})


def _uv_run(version: str, *args: str) -> list[str]:
    uv = uv_path() or "uv"
    return [uv, "tool", "run", "--python", "3.13", "--from", f"{PACKAGE}=={version}", PACKAGE, *args]


def _detached() -> dict[str, Any]:
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def relaunch(version: str) -> None:
    """Start the launcher again: it starts ``version`` and stops this copy."""
    env = {**os.environ, NO_OPEN_ENV: "1"}
    launcher = os.environ.get(LAUNCHER_ENV)
    if sys.platform != "win32" and launcher:
        command = ["/bin/bash", launcher]
    else:
        uv = uv_path() or "uv"
        command = [*_uv_run(version, "--launch", "--uv", uv)]
        port = os.environ.get("SCG_PORT")
        if port:
            command += ["--port", port]
    subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **_detached())


def _download(version: str) -> None:
    """Fetch the release (and its Python) now, so the switch-over is quick."""
    command = _uv_run(version, "--help")
    extra: dict[str, Any] = {}
    uv = Path(command[0])
    if uv.name.lower() == "uvw.exe" and (uv.parent / "uv.exe").is_file():
        # uvw has no console, so its output can't be read; uv.exe next to it can.
        command[0] = str(uv.parent / "uv.exe")
    if sys.platform == "win32":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(command, capture_output=True, text=True, timeout=900,
                            check=False, encoding="utf-8", errors="replace", **extra)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise RuntimeError("Couldn't download it: " + (" ".join(tail) or f"uv exited {result.returncode}"))


def start_install(version: str | None = None) -> dict[str, Any]:
    """Download and switch to the newest release in the background."""
    if not can_install():
        raise RuntimeError("This copy wasn't started from the Mac app or the Windows shortcut. " + how_to_update())
    target = version or latest(force=True)
    if not newer(target, current()):
        raise RuntimeError("You already have the newest version.")
    with _lock:
        if _install["state"] in ("downloading", "restarting"):
            return dict(_install)
        _install.update(state="downloading", error=None, version=target)
    threading.Thread(target=_run_install, args=(target,), daemon=True).start()
    return dict(_install)


def _run_install(version: str) -> None:
    try:
        _download(version)
        pinned_path().write_text(version + "\n", encoding="utf-8")
        _install.update(state="restarting")
        logger.info("Updating to %s", version)
        relaunch(version)
    except Exception as exc:  # noqa: BLE001 - shown to the user as is
        logger.warning("Update to %s failed: %s", version, exc)
        _install.update(state="failed", error=str(exc))


def pinned() -> str | None:
    """The version in ``app-version``, if it is a plain release number."""
    with contextlib.suppress(OSError):
        text = pinned_path().read_text(encoding="utf-8").strip()
        if parse(text):
            return text
    return None


def hand_over(argv: list[str]) -> bool:
    """For the Windows shortcut: start the newer release the app updated to.

    The shortcut always runs the version it was installed with. When the app
    has since updated itself (``app-version`` is newer), start that one with
    the same arguments instead, and return True so this copy exits.
    """
    target = pinned()
    if not can_install() or not newer(target, current()):
        return False
    try:
        subprocess.Popen(_uv_run(target, *argv), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached())
    except OSError as exc:
        logger.warning("Couldn't start %s (%s); starting %s", target, exc, current())
        return False
    return True
