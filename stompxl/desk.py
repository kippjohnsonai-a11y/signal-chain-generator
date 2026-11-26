"""The pedal desk: work on whatever preset is in a pedal slot, no song needed.

Opening a slot reads it off the pedal and turns it into the same chain spec an
import makes, but keeps it here, in memory, instead of in the song library. It
stands in for a version under the id :data:`DESK_ID`, so everything that works
on a version (the chain view, the close-up and its quick picks, live edit,
Explain) works on it unchanged.

Saving writes to the slot itself; "Save as song" is the only way it reaches
the library. Each person (relay scope) has their own desk, like live edit.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

from . import config, edit, engine, importer, xl

#: The version id the desk answers to. Library ids start at 1.
DESK_ID = -1

_lock = threading.RLock()
_desks: dict[str, dict[str, Any]] = {}


class DeskError(ValueError):
    pass


def _who() -> str:
    from stompxl.helixgen.device.relay import SCOPE

    return SCOPE.get()


def from_hlx(hlx: dict[str, Any], slot: int) -> dict[str, Any]:
    """A desk for the preset ``hlx`` read from ``slot``."""
    try:
        chain, notes = importer.hlx_to_chain(
            hlx, captures=xl.parse_captures(config.get_setting("nam_captures")))
    except importer.ImportError_ as exc:
        raise DeskError(str(exc)) from exc
    try:
        preset, warnings = engine.build_preset(chain)
    except Exception as exc:
        raise DeskError(f"The preset in that slot didn't convert: {exc}") from exc
    if note := edit.dsp_warning(chain):
        notes.append(note)
    return {"slot": slot, "chain": chain, "preset": preset, "notes": notes,
            "warnings": warnings, "read_at": time.time()}


def load(slot: int, read: Any = None) -> dict[str, Any]:
    """Read ``slot`` off the pedal and make it this person's desk."""
    from . import pedal

    hlx = (read or pedal.read_preset)(slot)
    desk = from_hlx(hlx, slot)
    with _lock:
        _desks[_who()] = desk
    return desk


def current() -> dict[str, Any] | None:
    with _lock:
        return _desks.get(_who())


def rebase(chain: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    """After a save: the slot now holds ``chain``, so edits start from it."""
    with _lock:
        desk = _desks.get(_who())
        if desk is None:
            raise DeskError("Open a preset from the pedal first.")
        desk.update(chain=copy.deepcopy(chain), preset=copy.deepcopy(preset))
        return desk


def forget() -> None:
    with _lock:
        _desks.pop(_who(), None)


def record() -> dict[str, Any] | None:
    """The desk shaped like a library version, for the endpoints that take one."""
    from . import pedal

    desk = current()
    if desk is None:
        return None
    name = desk["chain"].get("meta", {}).get("name") or ""
    slot = desk["slot"]
    return {
        "id": DESK_ID, "pedal": True, "song_id": None, "parent_id": None, "kind": "pedal",
        "label": f"{pedal.slot_label(slot)} {name}".strip(), "guitar_id": None, "rig_id": None,
        "options": {}, "brief": {}, "chain": copy.deepcopy(desk["chain"]),
        "preset": copy.deepcopy(desk["preset"]), "notes": "\n".join(desk["notes"]),
        "warnings": list(desk["warnings"]), "feedback": "", "user_notes": "", "tags": [],
        "pushed_slot": slot, "created_at": desk["read_at"],
    }
