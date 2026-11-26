"""Live edit: values changed in the editor play on the pedal straight away.

Like HX Edit, and only while it's switched on. Starting loads the slot this
version was sent to and checks the pedal holds the same chain; after that each
value edit goes over as one ``op 30`` set-value into the pedal's edit buffer,
which is what's playing. Nothing reaches flash until :func:`stop` with
``keep=True`` (``op 71``), except when a long session has to be renewed
(the sequence wrap), which saves the buffer first; :func:`stop` with
``keep=False`` then writes the original values back so the slot ends as it was.

Values and on/off switches go live, for the snapshot the pedal is playing (the
one the preset opens on). A block swap, add, remove or move changes the chain,
and a change to another snapshot would land in the wrong one, so both wait for
the full push; :meth:`LiveSession.update` says so and still sends the rest.

Every failure (opening, a drop mid-edit, saving or discarding) is kept for Send
diagnostics with the wire trace, and a drop is retried once in a fresh session.

The session holds the pedal: other pedal calls refuse while it's on (see
:func:`stompxl.pedal._exclusive`), and it lets go by itself after
:data:`IDLE_SECONDS` without an edit, so a closed tab can't keep it forever.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Let the pedal go after this long without an edit.
IDLE_SECONDS = 30 * 60

#: While live edit is on but nothing is being sent, read what the pedal has
#: queued this often. Left unread for about ten seconds, the pedal stopped
#: answering (Nov 23, #91 and #92: three keepalives queued, then the next
#: set-value timed out and the value after the reconnect was refused). Reading
#: alone did not keep it open (#99), so a keepalive is answered with the host's.
HEARTBEAT_SECONDS = 2.0


class LiveError(RuntimeError):
    pass


class LiveEnded(LiveError):
    """No live session for this version (it was stopped, timed out, or replaced)."""


@dataclass(frozen=True)
class _Target:
    """Where one tone block's values go on the pedal."""

    index: int
    symbol: Any
    model_sel: int
    model: str


#: Pseudo-parameter for a block's on/off in the snapshot the pedal is playing.
ON_KEY = "@on"


def playing_snapshot(preset: dict[str, Any]) -> int:
    """The snapshot the pedal opens this preset on (and so plays in live edit)."""
    tone = preset.get("data", {}).get("tone", {})
    current = (tone.get("global") or {}).get("@current_snapshot", 0)
    return current if isinstance(current, int) and f"snapshot{current}" in tone else 0


def _snapshot_state(tone: dict[str, Any], snap_name: str, name: str
                    ) -> tuple[bool | None, dict[str, Any]]:
    """A block's on/off and its snapshot-controlled values in one snapshot."""
    snap = tone.get(snap_name)
    if not isinstance(snap, dict) or not snap.get("@valid", True):
        return None, {}
    dsp_name, _, block_name = name.rpartition(".")
    state = ((snap.get("blocks") or {}).get(dsp_name) or {}).get(block_name)
    controlled = ((snap.get("controllers") or {}).get(dsp_name) or {}).get(block_name) or {}
    values = {k: v["@value"] for k, v in controlled.items()
              if isinstance(v, dict) and "@value" in v}
    return (state if isinstance(state, bool) else None), values


def _values(preset: dict[str, Any], snapshot: int = 0) -> dict[str, dict[str, Any]]:
    """What the pedal plays in ``snapshot``: ``{tone block name: {parameter: raw}}``,
    plus each block's ``@model`` and its on/off there (``@on``).

    A snapshot-controlled value is the snapshot's own, not the block's stored one.
    """
    from stompxl.helixgen.device.codec import tone_blocks

    tone = preset.get("data", {}).get("tone", {})
    out = {}
    for name, block in tone_blocks(preset):
        values = {k: v for k, v in block.items() if not k.startswith("@") or k == "@model"}
        state, controlled = _snapshot_state(tone, f"snapshot{snapshot}", name)
        values.update({k: v for k, v in controlled.items() if k in values})
        if state is not None:
            values[ON_KEY] = state
        out[name] = values
    return out


def _elsewhere(preset: dict[str, Any], snapshot: int) -> dict[tuple[str, str, int], Any]:
    """On/off and snapshot values in every *other* snapshot. The pedal isn't
    playing those, so a change there waits for Save & send. A block the
    snapshots leave alone has no on/off of its own in them."""
    from stompxl.helixgen.device.codec import tone_blocks

    tone = preset.get("data", {}).get("tone", {})
    out: dict[tuple[str, str, int], Any] = {}
    for number in sorted(int(k[8:]) for k in tone if k.startswith("snapshot") and k[8:].isdigit()):
        if number == snapshot:
            continue
        for name, block in tone_blocks(preset):
            state, controlled = _snapshot_state(tone, f"snapshot{number}", name)
            # A stomp (the boosts, the looper) keeps its on/off across snapshot
            # changes, so its stored per-snapshot state isn't something to wait for.
            if state is not None and not block.get("@no_snapshot_bypass"):
                out[(name, ON_KEY, number)] = state
            for key, raw in controlled.items():
                out[(name, key, number)] = raw
    return out


def _models(values: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {name: params.get("@model") for name, params in values.items()}


#: A slot record that isn't the usual block layout (``19: 6``) but holds a
#: block all the same: an IR block, whose model sits directly in the record
#: (``20: {8: ..., 9: ...}``) rather than under the model reference (``24``).
TAG_IR_BLOCK = 7


def _record(document: Any, index: int) -> dict | None:
    array = document.slot_array(0) if hasattr(document, "slot_array") else None
    if not array or index >= len(array) or not isinstance(array[index], dict):
        return None
    return array[index]


def _record_model(document: Any, index: int) -> set[int]:
    """The model ids a slot record could be naming (empty when it holds none)."""
    found = document.model_index(index)
    if found is not None:
        return {found}
    entry = _record(document, index)
    if entry and entry.get(19) == TAG_IR_BLOCK and isinstance(entry.get(20), dict):
        return {v for k, v in entry[20].items() if k in (8, 9) and isinstance(v, int)}
    return set()


def _occupied(document: Any) -> list[int]:
    """Slots holding a block of either layout."""
    slots = set(document.block_slots())
    array = document.slot_array(0) if hasattr(document, "slot_array") else None
    for index, entry in enumerate(array or []):
        if isinstance(entry, dict) and entry.get(19) == TAG_IR_BLOCK:
            slots.add(index)
    return sorted(slots)


def _describe(document: Any, index: int | None) -> str:
    if index is None:
        return "no slot for it"
    entry = _record(document, index)
    if entry is None:
        return f"slot {index}: model {document.model_index(index)}"
    content = entry.get(20)
    head = {k: v for k, v in content.items() if not isinstance(v, (dict, list))} \
        if isinstance(content, dict) else content
    return f"slot {index}: tag {entry.get(19)} model {document.model_index(index)} {head}"


class LiveSession:
    """One slot's edit buffer, held open while live edit is on."""

    def __init__(self, *, version_id: int, preset: dict[str, Any], slot: int, bank: int = 0,
                 symbols: Any, amps: Any, catalog: Any, open_run: Any = None):
        from stompxl.helixgen.device.document import parse
        from stompxl.helixgen.device.editor import (
            MODEL_MAIN,
            MODEL_PAIRED,
            EditSession,
            plan_chain,
        )

        self.version_id = version_id
        self.slot = slot
        self.bank = bank
        self.catalog = catalog
        self.name = preset.get("data", {}).get("meta", {}).get("name") or "StompXL"
        self._open_run = open_run or EditSession
        self.run = self._open_run(bank, slot, self.name)
        try:
            if self.run.document is None:
                raise LiveError("That slot is empty on the pedal. Send this version to it first.")
            document = parse(self.run.document)
            planned, _ = plan_chain(preset, symbols, amps)
            self.targets: dict[str, _Target] = {}
            used: set[int] = set()
            for position, entry in enumerate(planned):
                index = document.record_for(0, position)
                paired = entry["paired"]
                found = None if index is None else _record_model(document, index)
                if (index is None or entry["symbol"].index not in found
                        or (paired >= 0 and document.cab_index(index) != paired)):
                    held = [symbols.by_index(i) for i in sorted(found or ())] if symbols else []
                    logger.warning(
                        "Live edit: slot %s position %d (%s) should hold model %s (%s)%s; the "
                        "pedal has %s (%s)", slot, position, entry["name"],
                        entry["symbol"].index, getattr(entry["symbol"], "symbol", "?"),
                        f" + cab {paired}" if paired >= 0 else "", _describe(document, index),
                        ", ".join(h.symbol for h in held if h is not None) or "nothing")
                    raise LiveError(self._mismatch())
                used.add(index)
                self.targets[entry["name"]] = _Target(index, entry["symbol"], MODEL_MAIN,
                                                      entry["block"]["@model"])
                if entry["cab_name"] is not None and entry["paired_symbol"] is not None:
                    self.targets[entry["cab_name"]] = _Target(
                        index, entry["paired_symbol"], MODEL_PAIRED, entry["cab_block"]["@model"])
            extra = set(_occupied(document)) - used
            if extra:
                logger.warning("Live edit: slot %s also holds blocks the version doesn't: %s",
                               slot, ", ".join(_describe(document, i) for i in sorted(extra)))
                # Left over from the preset that was there before (#99). The
                # version doesn't have them, so clear them in the edit buffer:
                # Save keeps that, Discard reloads the slot with them.
                if not self._clear(sorted(extra)):
                    raise LiveError(self._mismatch())
        except Exception:
            self.run.close()
            raise
        #: The snapshot the pedal is on: selecting the slot opens it there.
        self.snapshot = playing_snapshot(preset)
        self.original = _values(preset, self.snapshot)
        self._original_elsewhere = _elsewhere(preset, self.snapshot)
        #: Values the pedal refused, so they're reported once, not on every edit.
        self._refused: dict[tuple[str, str], Any] = {}
        self.models = _models(self.original)
        #: What the edit buffer holds, and what flash holds.
        self.sent = copy.deepcopy(self.original)
        self.saved = copy.deepcopy(self.original)
        self.touched = time.monotonic()
        self.io_at = self.touched
        #: Keepalives answered while idle (see :meth:`heartbeat`).
        self.keepalives = 0

    def _clear(self, slots: list[int]) -> bool:
        from stompxl.helixgen.device.editor import BlockEditor, EditError

        for index in slots:
            try:
                BlockEditor(self.run.session).delete_block(index)
            except EditError:
                # The first edit on a fresh session can be refused once (#91).
                time.sleep(0.2)
                try:
                    BlockEditor(self.run.session).delete_block(index)
                except EditError as exc:
                    logger.warning("Live edit: could not clear slot %d: %s", index, exc)
                    return False
        logger.info("Live edit: cleared %d block(s) the version doesn't have: slot %s", len(slots),
                    ", ".join(str(i) for i in slots))
        return True

    def _mismatch(self) -> str:
        from .pedal import slot_label

        return (f"Slot {slot_label(self.slot)} on the pedal doesn't hold this version's chain "
                "(it was changed or overwritten). Use Save & send to pedal once, then turn "
                "Live on again.")

    # -- edits ---------------------------------------------------------------

    def update(self, preset: dict[str, Any]) -> dict[str, Any]:
        """Send every value that differs from what the pedal is playing.

        A block change (swap, add, remove, move) can't go live, but it doesn't
        stop the rest: values still go to every block whose model is where it
        was. Nor can a change to a snapshot the pedal isn't playing: an on/off
        sent now would switch the block in the snapshot it *is* playing.
        ``structural`` says some of the edits wait for Save & send, and
        ``waiting`` says which, in words.
        """
        self.touched = time.monotonic()
        wanted = _values(preset, self.snapshot)
        changed = {name for name, model in _models(wanted).items()
                   if self.models.get(name) != model}
        removed = set(self.models) - set(wanted)
        waiting = []
        if changed or removed:
            logger.info("Live edit: block changes wait for Save & send (%s)",
                        ", ".join(sorted(changed | removed)))
            waiting.append("block changes")
        elsewhere = _elsewhere(preset, self.snapshot)
        other = sorted({number + 1 for (name, key, number), raw in elsewhere.items()
                        if name not in changed and self._original_elsewhere.get(
                            (name, key, number), raw) != raw})
        if other:
            logger.info("Live edit: changes to snapshot(s) %s wait for Save & send (the "
                        "pedal is on snapshot %d)", ", ".join(map(str, other)),
                        self.snapshot + 1)
            waiting.append(f"snapshot {', '.join(map(str, other))} "
                           f"(the pedal is on snapshot {self.snapshot + 1})")
        sent, unsupported = 0, []
        for name, params in wanted.items():
            if name in changed:
                continue
            for key, raw in params.items():
                if key == "@model" or self.sent[name].get(key) == raw:
                    continue
                if self._refused.get((name, key), object()) == raw:
                    continue  # already reported
                if self._send(name, key, raw):
                    sent += 1
                    self._refused.pop((name, key), None)
                else:
                    self._refused[(name, key)] = raw
                    unsupported.append("on/off" if key == ON_KEY else key)
        if sent or unsupported:
            logger.info("Live edit on slot %s: %d value(s) sent%s", self.slot, sent,
                        f", not sent: {', '.join(unsupported)}" if unsupported else "")
        return {"sent": sent, "structural": bool(waiting), "unsupported": unsupported,
                "waiting": waiting}

    def _send(self, name: str, key: str, raw: Any) -> bool:
        from stompxl.helixgen.device.editor import EditError

        try:
            ok = self._set(name, key, raw)
        except EditError:
            raise
        except Exception as exc:  # noqa: BLE001 - USB or relay dropped: reconnect once
            logger.info("Live session dropped (%s); reconnecting", exc)
            _note(f"Live edit on {self._label()}: the pedal stopped answering (reconnected)", exc)
            self._reconnect()
            ok = self._set(name, key, raw)
            if not ok:
                # The first edit on a fresh session was refused once (#91);
                # the same value a moment later played.
                time.sleep(0.2)
                ok = self._set(name, key, raw)
        self.io_at = time.monotonic()
        if ok:
            self.sent[name][key] = raw
        return ok

    def _set(self, name: str, key: str, raw: Any) -> bool:
        from stompxl.helixgen.device.editor import BlockEditor, enum_index

        target = self.targets.get(name)
        if key == ON_KEY:
            return self._set_enabled(name, target, bool(raw))
        ordinal = target.symbol.ordinal_of(key) if target else None
        if ordinal is None:
            logger.info("Live edit: %s %s has no pedal parameter to send to", name, key)
            return False
        if isinstance(raw, str):
            raw = enum_index(self.catalog, target.model, key, raw)
            if raw is None:
                return False
        self._renew()
        ok = BlockEditor(self.run.session).try_set_value(target.index, ordinal, raw,
                                                         model_sel=target.model_sel)
        logger.info("Live edit: %s %s = %r -> slot %d param %d%s: %s", name, key, raw,
                    target.index, ordinal, " (cab)" if target.model_sel else "",
                    "played" if ok else "refused by the pedal")
        return ok

    def _set_enabled(self, name: str, target: _Target | None, on: bool) -> bool:
        """Switch a block on or off in the snapshot the pedal is playing."""
        from stompxl.helixgen.device.editor import MODEL_MAIN, BlockEditor, EditError

        if target is None or target.model_sel != MODEL_MAIN:
            # A cab rides in its amp's slot: it has no switch of its own.
            logger.info("Live edit: %s has no switch of its own on the pedal", name)
            return False
        self._renew()
        try:
            BlockEditor(self.run.session).set_enabled(target.index, on)
        except EditError as exc:
            logger.info("Live edit: %s %s -> slot %d refused by the pedal: %s", name,
                        "on" if on else "off", target.index, exc)
            return False
        logger.info("Live edit: %s %s -> slot %d: played", name, "on" if on else "off",
                    target.index)
        return True

    def _renew(self) -> None:
        before = self.run.sessions
        self.run.renew_if_needed()
        if self.run.sessions != before:
            # Renewing saved the buffer to flash.
            self.saved = copy.deepcopy(self.sent)

    def heartbeat(self) -> None:
        """Read what the pedal has queued, so an idle session stays open.

        If the pedal has already stopped answering, reconnect now rather than
        on the next edit, which would wait out a timeout first.
        """
        if time.monotonic() - getattr(self, "io_at", 0.0) < HEARTBEAT_SECONDS:
            return
        session = self.run.session
        settle = getattr(session, "settle", None)
        if settle is None or type(getattr(session, "_device", None)).__name__ == "RelayDevice":
            # Over the browser relay a read that times out stays pending on the
            # pipe and stalls the next write (see Session.push_document).
            return
        try:
            before = getattr(session, "keepalives_seen", 0)
            settle()
            if getattr(session, "keepalives_seen", 0) > before and hasattr(session, "keepalive"):
                # Reading them was not enough (#99): the pedal wants an answer.
                if getattr(session, "near_sequence_wrap", False):
                    # Saving would keep edits the player hasn't saved; a fresh session
                    # replays them instead.
                    logger.info("Live edit on %s: fresh session before the frame "
                                "counter wraps", self._label())
                    self._reconnect()
                else:
                    session.keepalive()
                    self.keepalives += 1
                    if self.keepalives == 1:
                        logger.info("Live edit on %s: answering the pedal's keepalives",
                                    self._label())
        except Exception as exc:  # noqa: BLE001 - a dead session: start a fresh one
            logger.info("Live session went quiet (%s); reconnecting", exc)
            _note(f"Live edit on {self._label()}: the pedal stopped answering while idle "
                  "(reconnected)", exc)
            self._reconnect()
        self.io_at = time.monotonic()

    def _reconnect(self) -> None:
        """A fresh session reloads the slot from flash, so replay what flash lacks."""
        self._fresh()
        self._restore(self.saved, self.sent)

    def _restore(self, have: dict[str, dict[str, Any]], want: dict[str, dict[str, Any]]) -> None:
        for name, params in want.items():
            for key, raw in params.items():
                if key != "@model" and have[name].get(key) != raw:
                    self._set(name, key, raw)

    # -- ending ----------------------------------------------------------------

    def _label(self) -> str:
        from .pedal import slot_label

        return f"slot {slot_label(self.slot)}"

    def _fresh(self) -> None:
        """A new session on the slot, as flash holds it."""
        with contextlib.suppress(Exception):  # it's already gone
            self.run.close()
        self.run = self._open_run(self.bank, self.slot, self.name)

    def keep(self) -> None:
        """Save what's playing to the slot."""
        try:
            self.run.commit()
        except Exception as exc:  # noqa: BLE001 - dropped while idle: reconnect once
            logger.info("Live session dropped before saving (%s); reconnecting", exc)
            _note(f"Live edit on {self._label()}: saving (reconnected)", exc)
            self._reconnect()
            self.run.commit()
        self.saved = copy.deepcopy(self.sent)

    def rebase(self, preset: dict[str, Any]) -> None:
        """The slot now holds ``preset`` (saved): edits and Discard start from it."""
        self.original = _values(preset, self.snapshot)
        self._original_elsewhere = _elsewhere(preset, self.snapshot)
        self.models = _models(self.original)
        self._refused.clear()

    def discard(self) -> None:
        """Put the slot back as it was before live edit started."""
        from stompxl.helixgen.device.editor import BlockEditor

        try:
            self._discard()
        except Exception as exc:  # noqa: BLE001 - dropped while idle: reconnect once
            logger.info("Live session dropped before discarding (%s); reconnecting", exc)
            _note(f"Live edit on {self._label()}: discarding (reconnected)", exc)
            # A fresh session reloads the slot from flash, which is the discard
            # unless a renewal saved edits there.
            self._fresh()
            self.sent = copy.deepcopy(self.saved)
            if self.saved != self.original:
                self._discard()
            else:
                BlockEditor(self.run.session).select_preset(self.bank, self.slot)

    def _discard(self) -> None:
        from stompxl.helixgen.device.editor import BlockEditor

        if self.saved != self.original:
            # A renewal saved some edits; write the originals back over them.
            self._restore(self.sent, self.original)
            self.sent = copy.deepcopy(self.original)
            self.run.commit()
            self.saved = copy.deepcopy(self.original)
        else:
            # Reload the slot from flash, so the pedal stops playing the edits.
            BlockEditor(self.run.session).select_preset(self.bank, self.slot)

    def close(self) -> None:
        try:
            self.run.close()
        except Exception as exc:  # noqa: BLE001 - closing a dead session
            logger.debug("Closing the live session: %s", exc)


def _note(what: str, exc: BaseException) -> None:
    """Keep a live edit problem for Send diagnostics, with the wire trace."""
    from . import pedal

    with contextlib.suppress(Exception):  # diagnostics must never break live edit
        pedal.diagnostics_note(what, exc)


# -- one live session per person ----------------------------------------------
#
# Each person using the app has their own pedal, so each can have a live
# session. Keyed by the relay scope (see :mod:`stompxl.users`).

_lock = threading.RLock()
_sessions: dict[str, LiveSession] = {}
_timers: dict[str, threading.Timer] = {}


def _who() -> str:
    from stompxl.helixgen.device.relay import SCOPE

    return SCOPE.get()


def _arm_timer() -> None:
    who = _who()
    old = _timers.pop(who, None)
    if old is not None:
        old.cancel()
    timer = threading.Timer(IDLE_SECONDS, _idle, args=(who,))
    timer.daemon = True
    _timers[who] = timer
    timer.start()


def _idle(who: str) -> None:
    from stompxl.helixgen.device.relay import SCOPE

    token = SCOPE.set(who)
    try:
        with _lock:
            session = _sessions.get(who)
            if session is not None and time.monotonic() - session.touched >= IDLE_SECONDS - 1:
                logger.info("Live edit idle for %d minutes; letting the pedal go",
                            IDLE_SECONDS // 60)
                # Neither saved nor reverted: the pedal keeps playing what it has.
                _release()
    finally:
        SCOPE.reset(token)


def _beat(who: str, session: LiveSession) -> None:
    """Keep ``session`` open until it's released."""
    from stompxl.helixgen.device.relay import SCOPE

    token = SCOPE.set(who)
    try:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            with _lock:
                if _sessions.get(who) is not session:
                    return
                try:
                    session.heartbeat()
                except Exception as exc:  # noqa: BLE001 - the reconnect failed too
                    logger.warning("Live edit: the pedal stopped answering (%s); letting go", exc)
                    _note(f"Live edit on {session._label()}: gave up while idle", exc)
                    _release()
                    return
    finally:
        SCOPE.reset(token)


def _start_heartbeat(session: LiveSession) -> None:
    thread = threading.Thread(target=_beat, args=(_who(), session), daemon=True,
                              name="live-heartbeat")
    thread.start()


def _release() -> None:
    from . import pedal

    who = _who()
    session = _sessions.pop(who, None)
    if session is not None:
        session.close()
    timer = _timers.pop(who, None)
    if timer is not None:
        timer.cancel()
    pedal._live.clear()


def current() -> LiveSession | None:
    with _lock:
        return _sessions.get(_who())


def start(version_id: int, preset: dict[str, Any], slot: int, *, bank: int = 0,
          make: Any = None) -> LiveSession:
    """Take the pedal and open ``slot`` for live edits. Replaces any earlier session."""
    from . import pedal

    with _lock:
        old = _sessions.get(_who())
        if old is not None:
            with contextlib.suppress(Exception):
                old.discard()
            _release()
        if not pedal._LOCK.acquire(timeout=30):
            raise pedal.PedalError("The pedal is busy with another job. Try again in a moment.")
        try:
            pedal._live.set()
        finally:
            pedal._LOCK.release()
        try:
            try:
                session = (make or _make)(version_id, preset, slot, bank)
            except LiveError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dropped read: one fresh try
                # Opening only selects and reads the slot, so trying again is
                # harmless; on Nov 22 a second tap always worked.
                logger.info("Opening live edit failed (%s); trying once more", exc)
                _note(f"Starting live edit on slot {pedal.slot_label(slot)} (retried)", exc)
                time.sleep(0.3)
                session = (make or _make)(version_id, preset, slot, bank)
        except Exception as exc:
            pedal._live.clear()
            if not isinstance(exc, LiveError):
                _note(f"Starting live edit on slot {pedal.slot_label(slot)}", exc)
            raise
        _sessions[_who()] = session
        _arm_timer()
        _start_heartbeat(session)
        return session


def _make(version_id: int, preset: dict[str, Any], slot: int, bank: int) -> LiveSession:
    from . import engine, pedal

    return LiveSession(version_id=version_id, preset=preset, slot=slot, bank=bank,
                       symbols=pedal.symbols(), amps=pedal._amp_defaults(),
                       catalog=engine.catalog())


def update(version_id: int, preset: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        session = _sessions.get(_who())
        if session is None or session.version_id != version_id:
            raise LiveEnded("Live edit is off (it lets go of the pedal after 30 idle minutes). "
                            "Tick Live on pedal to start again.")
        try:
            result = session.update(preset)
        except Exception as exc:
            _note(f"Live edit on {session._label()}: gave up", exc)
            _release()
            raise
        _arm_timer()
        return result


def save(version_id: int, preset: dict[str, Any]) -> dict[str, Any]:
    """Send what's left of ``preset``, save it to the slot, and stay live on it.

    Only for value edits: a block change isn't on the pedal, so that needs a
    full push instead (``structural`` in the answer says so, and nothing is saved).
    """
    with _lock:
        session = _sessions.get(_who())
        if session is None or session.version_id != version_id:
            raise LiveEnded("Live edit is off (it lets go of the pedal after 30 idle minutes).")
        try:
            result = session.update(preset)
            if result["structural"]:
                return result
            session.keep()
            session.rebase(preset)
        except Exception as exc:
            _note(f"Saving live edits on {session._label()}", exc)
            _release()
            raise
        _arm_timer()
        return result


def stop(*, keep: bool) -> int | None:
    """End live edit, saving to the slot or reverting it. Returns the slot, if one was live."""
    with _lock:
        session = _sessions.get(_who())
        if session is None:
            return None
        slot = session.slot
        try:
            if keep:
                session.keep()
            else:
                session.discard()
        except Exception as exc:
            _note(f"Ending live edit on {session._label()} ({'save' if keep else 'discard'})",
                  exc)
            raise
        finally:
            _release()
        return slot

