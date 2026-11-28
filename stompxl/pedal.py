"""The pedal over USB, through HelixGen's reverse-engineered protocol.

Pushing overwrites a slot, so every push first archives what the slot held to
the backups directory. HX Edit must be installed (its symbol table maps model
names to the ids the pedal uses on the wire) but closed while pushing, since
only one program can hold the USB interface.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import config, engine, xl

logger = logging.getLogger(__name__)




class _PerPerson:
    """One of something for each person using the app, each with their own pedal."""

    def __init__(self, make: Any):
        self._make = make
        self._items: dict[str, Any] = {}
        self._guard = threading.Lock()

    def mine(self) -> Any:
        from stompxl.helixgen.device.relay import SCOPE

        with self._guard:
            key = SCOPE.get()
            if key not in self._items:
                self._items[key] = self._make()
            return self._items[key]


class _PerPersonLock(_PerPerson):
    def __init__(self) -> None:
        super().__init__(threading.Lock)

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        return self.mine().acquire(*args, **kwargs)

    def release(self) -> None:
        self.mine().release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


class _PerPersonEvent(_PerPerson):
    def __init__(self) -> None:
        super().__init__(threading.Event)

    def is_set(self) -> bool:
        return self.mine().is_set()

    def set(self) -> None:
        self.mine().set()

    def clear(self) -> None:
        self.mine().clear()


#: One USB conversation at a time with each person's pedal.
_LOCK = _PerPersonLock()
#: Set while live edit holds that person's pedal (see :mod:`stompxl.live`).
_live = _PerPersonEvent()
LIVE_BUSY = ("Live edit is holding the pedal. Save or discard your edits (or untick "
             "Live on pedal) first.")

# pyusb locates libusb through ctypes, which on Apple Silicon does not look in
# Homebrew's /opt/homebrew/lib. The fallback path is read at lookup time, so
# adding it here is enough for `brew install libusb` to be found.
if sys.platform == "darwin":
    _paths = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":")
    for _brew in ("/opt/homebrew/lib", "/usr/local/lib"):
        if _brew not in _paths:
            _paths.append(_brew)
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(p for p in _paths if p)


class PedalError(RuntimeError):
    pass


@contextlib.contextmanager
def _exclusive() -> Any:
    """The USB lock, refused outright while live edit holds the pedal."""
    if _live.is_set():
        raise PedalError(LIVE_BUSY)
    with _LOCK:
        if _live.is_set():
            raise PedalError(LIVE_BUSY)
        yield


def symbols() -> Any:
    """HX Edit's model table, which maps model names to the ids the pedal uses."""
    from stompxl.helixgen.device.hxedit import find_symbol_table
    from stompxl.helixgen.device.symbols import DeviceSymbols

    try:
        return DeviceSymbols.load(find_symbol_table(None))
    except Exception as exc:
        raise PedalError(
            f"HX Edit's symbol table was not found ({exc}). Install HX Edit, then retry."
        ) from exc


@dataclass
class DeviceStatus:
    connected: bool
    description: str = ""
    verified: bool = False
    error: str = ""
    #: Where the pedal is plugged in: "" for this Mac, or the device relaying it.
    where: str = ""


def status() -> DeviceStatus:
    try:
        from stompxl.helixgen.device.usb import find_devices

        devices = find_devices()
    except Exception as exc:  # noqa: BLE001 - no pyusb/libusb is a status, not a crash
        return DeviceStatus(connected=False, error=str(exc))
    if not devices:
        return DeviceStatus(connected=False)
    info = devices[0]
    from stompxl.helixgen.device import relay

    remote = relay.current()
    from stompxl import xl

    # A Stomp or a Stomp XL plugged in sets which pedal presets are made for.
    xl.remember_pedal(info.product_id)
    return DeviceStatus(
        connected=True,
        description=info.describe(),
        where=remote[2] if remote and remote[0] == info else "",
        verified=info.verified,
    )


def slot_label(slot: int) -> str:
    """Slot 0 is 1A on the pedal: four presets (A-D) per bank on the XL, three on the Stomp."""
    from stompxl import xl

    size = xl.BANK_SIZE
    return f"{slot // size + 1}{'ABCD'[slot % size]}"


def _timed_out(exc: BaseException) -> bool:
    """Whether a USB failure was the pedal not answering in time.

    Worth one fresh try: a new session re-claims the interface and drains
    whatever was left over, which is what clears a stalled conversation.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if getattr(seen, "timeout", False) or type(seen).__name__ == "USBTimeoutError":
            return True
        if "timed out" in str(seen).lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _with_retry[T](work: Callable[[Any], T]) -> T:
    """Run ``work`` in a pedal session, once more in a fresh one after a timeout.

    Only for reads: nothing on the pedal changes, so trying again is harmless.
    """
    from stompxl.helixgen.device.usb import Session

    try:
        with Session.open() as session:
            return work(session)
    except Exception as exc:
        if not _timed_out(exc):
            raise
        logger.warning("Pedal read timed out (%s); retrying in a fresh session", exc)
    time.sleep(0.3)
    with Session.open() as session:
        return work(session)


NOT_ANSWERING = ("The pedal stopped answering. Unplug it and plug it back in, tap "
                 "Connect pedal, then try again.")


def _log_trace(what: str, exc: BaseException) -> None:
    """Log the USB traffic that led up to a failure, to see where it stalled."""
    from stompxl.helixgen.device import relay
    from stompxl.helixgen.device.usb import frame_trace_text

    from . import diagnostics

    frames = frame_trace_text()
    relayed = relay.trace_text() if relay.current() is not None else ""
    logger.warning("%s failed (%s). Last frames on the wire:\n%s", what, exc, frames)
    if relayed:
        logger.warning("Last relayed USB calls:\n%s", relayed)
    # Kept for the app's Send diagnostics button, so nobody needs the log.
    diagnostics.record_failure(what, exc, frames=frames, relayed=relayed)


def diagnostics_note(what: str, error: BaseException | str) -> None:
    """Keep a pedal problem for Send diagnostics, with the traffic before it."""
    from stompxl.helixgen.device import relay
    from stompxl.helixgen.device.usb import frame_trace_text

    from . import diagnostics

    diagnostics.record_failure(
        what, error, frames=frame_trace_text(),
        relayed=relay.trace_text() if relay.current() is not None else "")


def _read_error(slot: int, exc: BaseException) -> PedalError:
    _log_trace(f"Reading slot {slot_label(slot)}", exc)
    hint = f". {NOT_ANSWERING}" if _timed_out(exc) else ""
    return PedalError(f"Could not read slot {slot_label(slot)}: {exc}{hint}")


#: The last preset listing each person's pedal gave (see :func:`known_slots`).
_listings: dict[str, dict[int, str]] = {}


def _remember(names: dict[int, str]) -> dict[int, str]:
    from stompxl.helixgen.device.relay import SCOPE

    _listings[SCOPE.get()] = dict(names)
    return names


def known_slots() -> dict[int, str] | None:
    """The preset names last read off this person's pedal, without asking it again
    (live edit holds the pedal, and the pedal pane still wants its list)."""
    from stompxl.helixgen.device.relay import SCOPE

    found = _listings.get(SCOPE.get())
    return dict(found) if found is not None else None


def list_slots(bank: int = 0) -> dict[int, str]:
    """Preset names by slot, from the listing the pedal's own browser uses."""

    with _exclusive():
        try:
            raw = _with_retry(lambda session: session.list_presets(bank=bank))
        except Exception as exc:
            _log_trace("Listing the pedal's presets", exc)
            hint = f". {NOT_ANSWERING}" if _timed_out(exc) else ""
            raise PedalError(f"{exc}{hint}") from exc
    return _remember(_parse_listing(raw))


def read_preset(slot: int, *, bank: int = 0) -> dict[str, Any]:
    """The preset in ``slot`` as an .hlx-shaped dict. Read-only: nothing is written."""
    from stompxl.helixgen.device.document import parse
    from stompxl.helixgen.device.hxedit import find_symbol_table
    from stompxl.helixgen.device.symbols import DeviceSymbols

    from . import pedal_import

    with _exclusive():
        try:
            symbols = DeviceSymbols.load(find_symbol_table(None))
        except Exception as exc:
            raise PedalError(
                f"HX Edit's symbol table was not found ({exc}). Install HX Edit, then retry."
            ) from exc
        try:
            raw, listing = _with_retry(lambda session: (
                session.read_slot(bank=bank, slot=slot), session.list_presets(bank=bank)))
        except Exception as exc:
            raise _read_error(slot, exc) from exc
        names = _remember(_parse_listing(listing))
    if not raw:
        raise PedalError(f"Slot {slot_label(slot)} is empty.")
    # Kept alongside the push backups, in case an import ever needs checking.
    (config.backups_dir() / f"import-slot-{slot:03d}-{time.strftime('%Y%m%d-%H%M%S')}.msgpack"
     ).write_bytes(raw)
    return pedal_import.document_to_hlx(parse(raw), symbols, engine.catalog(),
                                        names.get(slot, "") or f"Slot {slot_label(slot)}")


#: The name a slot shows when nothing has been saved there.
EMPTY_NAMES = frozenset({"", "new preset"})


def read_all(report: Callable[[str], None] = lambda _m: None, *, bank: int = 0
             ) -> dict[int, tuple[str, dict[str, Any]]]:
    """Every saved preset on the pedal as ``{slot: (name, .hlx-shaped dict)}``, in one session.

    Read-only, like :func:`read_preset`, but nothing is archived: it's for looking
    across the presets (which NAM captures they use), not for importing one.
    A preset that won't read or convert is skipped, not fatal.
    """
    from stompxl.helixgen.device.document import parse
    from stompxl.helixgen.device.hxedit import find_symbol_table
    from stompxl.helixgen.device.symbols import DeviceSymbols
    from stompxl.helixgen.device.usb import Session

    from . import pedal_import

    try:
        symbols = DeviceSymbols.load(find_symbol_table(None))
    except Exception as exc:
        raise PedalError(
            f"HX Edit's symbol table was not found ({exc}). Install HX Edit, then retry."
        ) from exc
    raws: dict[int, bytes] = {}
    with _exclusive():
        try:
            with Session.open() as session:
                names = _remember(_parse_listing(session.list_presets(bank=bank)))
                filled = [s for s, n in sorted(names.items()) if n.strip().lower() not in EMPTY_NAMES]
                for done, slot in enumerate(filled, 1):
                    report(f"Reading preset {done} of {len(filled)}: {names[slot]}")
                    try:
                        raw = session.read_slot(bank=bank, slot=slot)
                    except Exception as exc:
                        if _timed_out(exc):
                            raise
                        logger.warning("Skipped slot %s while reading them all: %s", slot_label(slot), exc)
                        continue
                    if raw:
                        raws[slot] = raw
        except Exception as exc:
            _log_trace("Reading every preset", exc)
            hint = f". {NOT_ANSWERING}" if _timed_out(exc) else ""
            raise PedalError(f"{exc}{hint}") from exc
    found: dict[int, tuple[str, dict[str, Any]]] = {}
    for slot, raw in raws.items():
        name = names.get(slot, "") or f"Slot {slot_label(slot)}"
        try:
            found[slot] = (name, pedal_import.document_to_hlx(parse(raw), symbols, engine.catalog(), name))
        except Exception as exc:  # noqa: BLE001 - one odd preset shouldn't stop the rest
            logger.warning("Skipped slot %s: it didn't convert (%s)", slot_label(slot), exc)
    return found


def push(preset: dict[str, Any], slot: int, *, bank: int = 0,
         device_settings: dict[str, Any] | None = None) -> str:
    """Archive ``slot``, then write ``preset`` into it. Returns HelixGen's report."""
    from stompxl.helixgen.device.editor import DspBudgetError, apply_tone
    from stompxl.helixgen.device.hxedit import find_symbol_table
    from stompxl.helixgen.device.symbols import DeviceSymbols

    with _exclusive():
        try:
            symbols = DeviceSymbols.load(find_symbol_table(None))
        except Exception as exc:
            raise PedalError(
                f"HX Edit's symbol table was not found ({exc}). Install HX Edit, then retry."
            ) from exc

        try:
            donor, packet = _with_retry(lambda session: (
                session.read_slot(bank=bank, slot=slot), session.out_packet_size))
        except Exception as exc:
            raise _read_error(slot, exc) from exc
        if packet < 512:
            # Before anything changes: at full speed the chain edits would go
            # through and the snapshot/footswitch write would then stall.
            from stompxl.helixgen.device.writer import SLOW_LINK

            logger.warning("Send refused: the pedal's OUT packets are %s B (full speed)", packet)
            diagnostics_note(f"Sending to slot {slot_label(slot)}", SLOW_LINK)
            raise PedalError(SLOW_LINK)
        if donor is None:
            raise PedalError(
                f"Slot {slot_label(slot)} is empty. Pick a slot that already holds a preset "
                "(it gets overwritten)."
            )
        archive = config.backups_dir() / f"slot-{slot:03d}-{time.strftime('%Y%m%d-%H%M%S')}.msgpack"
        archive.write_bytes(donor)

        name = preset.get("data", {}).get("meta", {}).get("name") or "StompXL"
        try:
            report = apply_tone(
                preset, symbols, bank=bank, slot=slot, name=name,
                catalog=engine.catalog(), amps=_amp_defaults(),
                device_settings=device_settings,
            )
        except DspBudgetError as exc:
            # Teach the budget check what this pedal actually holds.
            learned = xl.learn_dsp_refusal(getattr(exc, "placed", []),
                                           getattr(exc, "refused", ""), engine.catalog(),
                                           config.read_settings())
            if learned is not None:
                config.write_settings({"dsp_budget": learned})
            error = PedalError(
                f"{exc} Use \"Make it fit\" for a lighter version, or tweak it yourself."
            )
            refused = getattr(exc, "refused", "")
            model = xl.model_for_symbol(refused, engine.catalog()) if refused else None
            error.dsp_refused = model.display_name if model else refused
            raise error from exc
        except Exception as exc:
            _log_trace(f"Sending to slot {slot_label(slot)}", exc)
            raise PedalError(f"Upload failed: {exc}. The previous preset is in {archive}.") from exc
    notes = [f"Previous preset archived to {archive.name}.", report.summary()]
    if report.layout_document:
        # What the layout write sent (or tried to), next to the archive.
        (config.backups_dir() / archive.name.replace("slot-", "layout-slot-")
         ).write_bytes(report.layout_document)
    if report.layout_error:
        _log_trace(f"Writing snapshots and footswitches to slot {slot_label(slot)}",
                   RuntimeError(report.layout_error))
        notes.insert(0, (
            "The sound is on the pedal, but its snapshots and footswitch assignments "
            f"didn't go through ({report.layout_error}). Turn the pedal off and on "
            "before the next send."))
    tone = preset.get("data", {}).get("tone", {})
    gate = tone.get("dsp0", {}).get("inputA", {})
    if gate.get("noiseGate") and not report.input_gate:
        notes.append(
            f"Input noise gate: set it on the pedal (Input block > Gate on, Threshold "
            f"{gate.get('threshold', -55):g} dB). This preset's Input block didn't have the layout "
            "the app knows, so it couldn't be sent over USB; the downloaded .hlx has it."
        )
    return "\n".join(notes)


def link_text(session: Any) -> str:
    """How the pedal is attached: USB speed and the OUT endpoint's packet size.

    A document write is sized for 512-byte packets. A pedal that came up at
    full speed (64-byte packets, e.g. behind a USB 1.1 hub) takes data
    differently, which is worth knowing before blaming the document.
    """
    device = getattr(session, "_device", None)
    if device is None:
        return "link unknown"
    if type(device).__name__ == "RelayDevice":
        size = getattr(device, "out_packet_size", None)
        return f"relayed through a browser, OUT packets {size or 'unknown'} B"
    speeds = {1: "low speed", 2: "full speed (12 Mbit/s)", 3: "high speed (480 Mbit/s)",
              4: "super speed"}
    parts = []
    try:
        parts.append(speeds.get(device.speed, f"speed {device.speed}"))
    except Exception:  # noqa: BLE001 - informational only
        parts.append("speed unknown")
    try:
        from stompxl.helixgen.device.usb import CONTROL_INTERFACE, ENDPOINT_OUT

        iface = device.get_active_configuration()[(CONTROL_INTERFACE, 0)]
        parts.extend(f"OUT packets {endpoint.wMaxPacketSize} B" for endpoint in iface
                     if endpoint.bEndpointAddress == ENDPOINT_OUT)
    except Exception as exc:  # noqa: BLE001
        parts.append(f"endpoint unknown ({exc})")
    return ", ".join(parts)


def write_test(slot: int, *, bank: int = 0) -> dict[str, Any]:
    """Find out which part of a send the pedal refuses, without changing anything.

    Each step loads ``slot``, reads it (which opens its edit buffer) and writes a
    document into that buffer with the same op 21 a send uses, then loads the
    slot again. Nothing is saved: without op 71 the edit buffer is discarded.

    1. The document exactly as the pedal sent it.
    2. The same document re-encoded the way the app encodes what it sends.
    3. The snapshots-and-footswitches document from this slot's last send.

    The steps stop at the first failure, since a failed write leaves the pedal
    needing a power cycle. If step 1 fails, the document is not the problem.
    """
    from stompxl.helixgen.device.document import dump, parse
    from stompxl.helixgen.device.editor import BlockEditor
    from stompxl.helixgen.device.usb import Session
    from stompxl.helixgen.device.writer import DocumentWriter

    label = slot_label(slot)
    layouts = sorted(config.backups_dir().glob(f"layout-slot-{slot:03d}-*.msgpack"))
    steps: list[dict[str, Any]] = []
    link = ""

    def attempt(name: str, document: Callable[[bytes], bytes | None]) -> bool:
        nonlocal link
        started = time.monotonic()
        try:
            with Session.open() as session:
                link = link or link_text(session)
                editor = BlockEditor(session)
                editor.select_preset(bank, slot)
                raw = session.read_slot(bank=bank, slot=slot)
                if not raw:
                    raise PedalError(f"Slot {label} is empty")
                blob = document(raw)
                if blob is None:
                    steps.append({"step": name, "ok": None, "detail": "skipped: nothing to send"})
                    return True
                stats = DocumentWriter(session).write_document(blob, slot=slot, bank=bank)
                # Discard the edit buffer: nothing was committed.
                editor.select_preset(bank, slot)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            _log_trace(f"Write test on {label}, {name}", exc)
            steps.append({"step": name, "ok": False, "detail": str(exc) or type(exc).__name__})
            return False
        steps.append({"step": name, "ok": True, "detail": (
            f"{len(blob)} B in {stats.units} unit(s), worst credit "
            f"{stats.worst_non_final_latency_ms:.0f} ms, {time.monotonic() - started:.1f} s")})
        return True

    with _exclusive():
        for name, document in (
            ("as read", lambda raw: raw),
            ("re-encoded", lambda raw: dump(parse(raw))),
            ("last send's layout", lambda raw: layouts[-1].read_bytes() if layouts else None),
        ):
            if not attempt(name, document):
                break
            time.sleep(0.3)

    lines = [f"Pedal write test on slot {label} ({link or 'not connected'}):"]
    lines += [f"- {s['step']}: {'ok' if s['ok'] else 'skipped' if s['ok'] is None else 'FAILED'}"
              f" ({s['detail']})" for s in steps]
    if any(s["ok"] is False for s in steps):
        lines.append("Turn the pedal off and on before the next send.")
    summary = "\n".join(lines)
    logger.info("%s", summary)
    return {"slot": slot, "link": link, "steps": steps, "summary": summary}


def _amp_defaults() -> Any:
    try:
        from stompxl.helixgen.device.amps import AmpDefaults

        return AmpDefaults.load()
    except Exception as exc:  # noqa: BLE001 - optional: amps keep a separate cab
        logger.info("Amp defaults unavailable: %s", exc)
        return None


def _parse_listing(raw: bytes) -> dict[int, str]:
    """``{<slot>: {109: name}}`` rows, as HelixGen's desktop app decodes them."""
    import msgpack

    rows: Any = None
    try:
        envelope = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        rows = envelope.get(104) if isinstance(envelope, dict) else envelope
    except Exception:  # noqa: BLE001 - fall back to a tolerant streaming walk
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        unpacker.feed(raw)
        rows = []
        with contextlib.suppress(Exception):  # a malformed tail ends the walk
            rows.extend(unpacker)

    names: dict[int, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or len(row) != 1:
            continue
        ((slot, entry),) = row.items()
        if isinstance(slot, int) and isinstance(entry, dict):
            value = entry.get(109)
            if isinstance(value, bytes):
                value = value.decode("ascii", "replace")
            if isinstance(value, str) and value.split("\x00", 1)[0].strip():
                names[slot] = value.split("\x00", 1)[0].strip()
    return names
