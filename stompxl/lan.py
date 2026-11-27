"""Use the app from a phone on the same Wi-Fi: pair once with a QR code.

Off by default. Turned on (More > Phones on this Wi-Fi), the app also
listens on :data:`DEFAULT_PORT` on every network interface, next to its usual
loopback-only address. Nothing on that port works without a device token:

1. On the computer, **Pair a phone** makes a one-time code (10 minutes, one
   use) and shows it as a QR code of ``http://<this computer>:<port>/pair?code=...``.
2. The phone opens it. The code is swapped for a long random device token in
   an HttpOnly cookie, and the phone is sent to the app.
3. Every later request on that port must carry a token on the paired list.
   Only a hash of each token is kept, in ``devices.json`` in the data folder,
   which a library export never includes. Forgetting a phone ends its access.

Pairing, the device list and the switch itself only work from the computer:
a paired phone can't pair another device. Too many wrong codes cancel every
code waiting to be used.

The LAN port is plain HTTP, so it's for a home network you trust. Browsers only
allow WebUSB on HTTPS or the computer itself, so the pedal stays on the
computer: a phone here browses and edits the library and sends through the
computer's own USB link.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8790
COOKIE = "scg_device"
CODE_SECONDS = 600
#: Wrong codes allowed before every waiting code is cancelled.
MAX_BAD_CODES = 10
#: Paired phones stay paired for a year after their last visit.
COOKIE_SECONDS = 365 * 86400
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L

_lock = threading.Lock()
_codes: dict[str, float] = {}
_bad = 0
_server: Any = None
_thread: threading.Thread | None = None
_port: int | None = None
_phone: contextvars.ContextVar[bool] = contextvars.ContextVar("via_phone", default=False)


@contextlib.contextmanager
def via_phone() -> Iterator[None]:
    """Mark the request as coming from a paired phone (set by the access gate)."""
    token = _phone.set(True)
    try:
        yield
    finally:
        _phone.reset(token)


def is_phone() -> bool:
    return _phone.get()


# -- settings and paired devices ----------------------------------------------

def enabled() -> bool:
    return bool(config.get_setting("lan_enabled"))


def port() -> int:
    value = config.get_setting("lan_port")
    return int(value) if isinstance(value, int) and 1024 <= value <= 65535 else DEFAULT_PORT


def _devices_path() -> Path:
    return config.base_dir() / "devices.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def devices() -> list[dict[str, Any]]:
    path = _devices_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except ValueError:
        logger.warning("devices.json is damaged; no phones are paired until it's fixed.")
        return []
    return [d for d in data if isinstance(d, dict) and d.get("hash")]


def _save(items: list[dict[str, Any]]) -> None:
    path = _devices_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def public_devices() -> list[dict[str, Any]]:
    """The paired list without its hashes, for the settings page."""
    return [{k: d.get(k) for k in ("id", "name", "paired_at", "last_seen")} for d in devices()]


def forget(device_id: str) -> bool:
    with _lock:
        items = devices()
        kept = [d for d in items if d.get("id") != device_id]
        _save(kept)
    return len(kept) != len(items)


def device_for(token: str) -> dict[str, Any] | None:
    """The paired device whose token this is, or None. Notes the visit, hourly."""
    if not token:
        return None
    digest = _hash(token)
    with _lock:
        items = devices()
        for item in items:
            if hmac.compare_digest(str(item["hash"]), digest):
                if time.time() - float(item.get("last_seen") or 0) > 3600:
                    item["last_seen"] = time.time()
                    _save(items)
                return item
    return None


# -- pairing codes --------------------------------------------------------------

def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    with _lock:
        now = time.time()
        for old in [c for c, until in _codes.items() if until < now]:
            del _codes[old]
        _codes[code] = now + CODE_SECONDS
    return code


def normalize_code(code: str) -> str:
    """A code as typed on a phone: any case, spaces, with or without the dash."""
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else (code or "").strip().upper()


def redeem(code: str, name: str) -> str | None:
    """Swap a pairing code for a new device token; None if it's wrong or used."""
    global _bad
    code = normalize_code(code)
    with _lock:
        until = _codes.pop(code, None)
        if until is None or until < time.time():
            _bad += 1
            if _bad >= MAX_BAD_CODES:
                logger.warning("Too many wrong pairing codes: every waiting code is cancelled.")
                _codes.clear()
                _bad = 0
            return None
        _bad = 0
        token = secrets.token_urlsafe(32)
        items = devices()
        items.append({"id": secrets.token_hex(4), "name": name[:60] or "Phone",
                      "hash": _hash(token), "paired_at": time.time(), "last_seen": time.time()})
        _save(items)
    return token


def device_name(user_agent: str) -> str:
    ua = user_agent or ""
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
                         ("Macintosh", "Mac"), ("Windows", "Windows PC")):
        if needle in ua:
            return name
    return "Phone"


# -- addresses ------------------------------------------------------------------

def local_ip() -> str | None:
    """This computer's address on the network it reaches the internet through."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent: this only picks a route
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def pair_url(code: str) -> str | None:
    ip = local_ip()
    return f"http://{ip}:{port()}/pair?code={code}" if ip else None


def qr_svg(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_inline(border=2, dark="#000", light="#fff", omitsize=True)


# -- the listener -----------------------------------------------------------------

def listening() -> bool:
    """Whether the listener is up and has its port (False if, say, the port was taken)."""
    return bool(_server is not None and _thread is not None and _thread.is_alive()
                and getattr(_server, "started", False))


def running_port() -> int | None:
    """The LAN port while the listener is up: the access gate checks requests on it."""
    return _port


def start(app: Any) -> None:
    """Listen on every interface at :func:`port`, in the background."""
    global _server, _thread, _port
    import uvicorn

    stop()
    chosen = port()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=chosen,
                                           log_level="warning"))
    server.install_signal_handlers = lambda: None  # the main server handles Ctrl+C
    thread = threading.Thread(target=server.run, daemon=True, name="lan-listener")
    _server, _thread, _port = server, thread, chosen
    thread.start()
    logger.info("Phones on this Wi-Fi: listening on port %d", chosen)


def stop() -> None:
    global _server, _thread, _port
    server, thread = _server, _thread
    _server, _thread, _port = None, None, None
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=5)
