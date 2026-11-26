"""A pedal plugged into some other device, reached through a byte relay.

The protocol in :mod:`helixgen.device.usb` only ever touches the hardware
through a handful of pyusb calls: ``write``, ``read`` and ``clear_halt`` on the
device, ``claim_interface`` / ``release_interface`` / ``dispose_resources`` from
``usb.util``, and ``usb.core.USBError``. This module provides stand-ins for
exactly those, backed by a :class:`Transport` that forwards each call somewhere
else -- in the app, a browser holding the pedal over WebUSB.

Everything above the transport -- framing, sequencing, the ``arg`` counter,
paging, retries -- stays in :class:`~helixgen.device.usb.Session`, unchanged.
The far end is deliberately dumb: it moves bytes and reports timeouts.

The far end keeps the interface claimed for as long as it is connected.
``claim_interface`` asks it to release and re-claim, which cancels any read
left pending from the previous session; ``release_interface`` is a no-op.
Sessions still open and close politely over the protocol itself.
"""

from __future__ import annotations

import collections
import contextvars
import threading
import time
import types
from typing import Any, Protocol

from stompxl.helixgen.device.usb import DeviceInfo


class RelayError(Exception):
    """A relayed USB call failed. Plays the part of ``usb.core.USBError``."""

    def __init__(self, message: str, errno: int | None = None, *, timeout: bool = False):
        super().__init__(message)
        self.errno = errno
        #: The far end gave up waiting on the pedal (as opposed to failing).
        self.timeout = timeout


class Transport(Protocol):
    """Carries one USB call to the far end and waits for its answer.

    ``request`` is a small dict (``op`` plus arguments, bytes already encoded as
    the transport sees fit); the answer is a dict with ``ok`` and, for reads,
    ``data`` as bytes. ``timeout_s`` bounds the whole round trip. A transport
    raises :class:`RelayError` when the far end is gone or never answers.
    """

    def call(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...


#: Slack added on top of a USB timeout for the network round trip.
ROUND_TRIP_SLACK_S = 10.0


#: The last relayed USB calls, oldest first, so a failure can be logged with
#: what led up to it: ``(seconds since epoch, op, endpoint, bytes, outcome, ms, head)``.
TRACE: collections.deque[tuple[float, str, int, int, str, int, str]] = collections.deque(maxlen=80)


def trace_text() -> str:
    """The recent relayed calls, one per line, for the log."""
    if not TRACE:
        return "(no relayed calls)"
    start = TRACE[0][0]
    return "\n".join(
        f"  +{at - start:7.3f}s {op:<10} ep{ep} {size:>4}B {outcome:<8} {ms:>5}ms {head}"
        for at, op, ep, size, outcome, ms, head in TRACE
    )


class RelayDevice:
    """Quacks like the slice of ``usb.core.Device`` that Session uses."""

    def __init__(self, transport: Transport):
        self._transport = transport
        #: What the far end's browser reported for the OUT endpoint, if anything.
        self.out_packet_size = int(getattr(transport, "packet_size", 0) or 0) or None
        #: How long recent writes took end to end. A small write finishes on
        #: the pedal at once, so this is the network round trip to the page.
        self._write_ms: collections.deque[float] = collections.deque(maxlen=16)

    @property
    def round_trip_ms(self) -> float:
        """The typical round trip to the page, from recent writes (0 if none yet)."""
        if not self._write_ms:
            return 0.0
        ordered = sorted(self._write_ms)
        return ordered[len(ordered) // 2]

    def _call(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        started = time.time()
        try:
            reply = self._transport.call(request, timeout_s=timeout_ms / 1000 + ROUND_TRIP_SLACK_S)
        except RelayError as exc:
            reply = {"ok": False, "error": str(exc)}
            self._trace(request, reply, started)
            raise
        self._trace(request, reply, started)
        if not reply.get("ok"):
            raise RelayError(str(reply.get("error") or "relayed USB call failed"),
                             errno=reply.get("errno"), timeout=bool(reply.get("timeout")))
        return reply

    @staticmethod
    def _trace(request: dict[str, Any], reply: dict[str, Any], started: float) -> None:
        sent = request.get("data")
        got = reply.get("data")
        payload = sent if isinstance(sent, bytes | bytearray) else got
        size = len(payload) if isinstance(payload, bytes | bytearray) else int(request.get("size") or 0)
        if reply.get("ok"):
            outcome = "ok"
        elif reply.get("timeout"):
            outcome = "TIMEOUT"
        else:
            outcome = "ERROR"
        head = bytes(payload[:28]).hex() if isinstance(payload, bytes | bytearray) else ""
        if not reply.get("ok"):
            head = str(reply.get("error") or "")[:80]
        TRACE.append((started, str(request.get("op")), int(request.get("ep") or 0), size,
                      outcome, round((time.time() - started) * 1000), head))

    def reclaim(self) -> None:
        """Ask the far end to re-claim the interface, cancelling any read left pending.

        Each session starts with this, the way a local session claims the
        interface afresh. A page from before the op existed says "unknown op";
        that is fine, it just keeps its old behaviour.
        """
        try:
            self._call({"op": "reclaim"}, 2000)
        except RelayError as exc:
            if "unknown op" not in str(exc):
                raise

    def write(self, endpoint: int, data: bytes, timeout: int) -> int:
        started = time.perf_counter()
        reply = self._call({"op": "write", "ep": endpoint & 0x7F, "data": bytes(data),
                            "timeout": timeout}, timeout)
        self._write_ms.append((time.perf_counter() - started) * 1000.0)
        return int(reply.get("written", len(data)))

    def read(self, endpoint: int, size: int, timeout: int) -> bytes:
        reply = self._call({"op": "read", "ep": endpoint & 0x7F, "size": size,
                            "timeout": timeout}, timeout)
        return bytes(reply.get("data") or b"")

    def clear_halt(self, endpoint: int) -> None:
        direction = "in" if endpoint & 0x80 else "out"
        self._call({"op": "clear_halt", "ep": endpoint & 0x7F, "direction": direction}, 2000)

    def is_kernel_driver_active(self, interface: int) -> bool:
        raise NotImplementedError  # the far end owns the interface already


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _claim(device: Any, _interface: int) -> None:
    reclaim = getattr(device, "reclaim", None)
    if reclaim is not None:
        reclaim()


#: Stand-in for the ``usb`` package, as far as Session reaches into it.
USB_MODULE = types.SimpleNamespace(
    core=types.SimpleNamespace(USBError=RelayError),
    util=types.SimpleNamespace(
        claim_interface=_claim, release_interface=_noop, dispose_resources=_noop,
    ),
)


#: Whose pedal this code is working with. The app sets it per request, so
#: several people can each relay their own pedal at once; "" is the owner of the
#: machine, the only one who may also use a pedal plugged into it directly.
SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("helix_relay_scope", default="")


def local_usb_allowed() -> bool:
    """Whether this caller may fall back to a pedal on this machine's own USB."""
    return SCOPE.get() == ""


class _Remote:
    """The relayed pedal currently attached for each scope, if any."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, tuple[DeviceInfo, Transport, str]] = {}

    def attach(self, info: DeviceInfo, transport: Transport, where: str) -> None:
        with self._lock:
            self._current[SCOPE.get()] = (info, transport, where)

    def detach(self, transport: Transport) -> None:
        with self._lock:
            for scope, (_info, attached, _where) in list(self._current.items()):
                if attached is transport:
                    del self._current[scope]

    def current(self) -> tuple[DeviceInfo, Transport, str] | None:
        with self._lock:
            return self._current.get(SCOPE.get())


REMOTE = _Remote()


def attach(info: DeviceInfo, transport: Transport, where: str = "") -> None:
    """Make ``info`` the pedal Session.open uses, in preference to local USB.

    The newest attach wins: plugging the pedal into a phone after a laptop
    moves the app to the phone. Each :data:`SCOPE` has its own.
    """
    REMOTE.attach(info, transport, where)


def detach(transport: Transport) -> None:
    """Forget the relayed pedal, if ``transport`` is still the one attached."""
    REMOTE.detach(transport)


def current() -> tuple[DeviceInfo, Transport, str] | None:
    """``(info, transport, where)`` for this scope's relayed pedal, or None."""
    return REMOTE.current()
