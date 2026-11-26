"""The player's NAM capture library: find on TONE3000, download, convert, list in settings.

Adding a capture:

1. download its A2 .nam from TONE3000, into ``<NAM folder>/nam`` (what the NAM
   plugin on the computer loads);
2. if it has a User IR slot, convert it into ``<NAM folder>/HX IRs/<slot> <name>.wav``
   (what NAM A2 Lite on the pedal plays; drag it onto that slot in HX Edit);
3. list it in Settings > NAM captures, so presets can use it.

HX Edit has no way in for other programs, so step 2's import is the one thing
left to do by hand.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from . import config, nam2ir, tone3000, users, xl

FOLDER_SETTING = "nam_folder"


def folder() -> Path:
    """Where the .nam files and the .wav files go: somewhere the player can find them."""
    chosen = config.get_setting(FOLDER_SETTING)
    if chosen:
        path = Path(chosen).expanduser()
    elif (sys.platform == "darwin" and not os.environ.get(config.DATA_DIR_ENV)
          and not users.is_guest()):
        path = Path.home() / "Music" / "StompXL NAM"
    else:
        path = config.data_dir() / "NAM"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ir_dir() -> Path:
    return folder() / "HX IRs"


def nam_dir() -> Path:
    return folder() / "nam"


#: The ready-made packs, by name: see docs/nam.md.
PACKS = {
    "starter": "nam_starter.json",    # slots 101-120, a mix of full rigs and amp-only
    "full-rig": "nam_full_rig.json",  # slots 121 up, full rigs only (no cab block needed)
}


def pack(name: str) -> list[dict[str, Any]]:
    """One ready-made pack's captures."""
    if name not in PACKS:
        raise ValueError(f"No pack called {name}: try {', '.join(PACKS)}")
    data = json.loads(resources.files("stompxl").joinpath(PACKS[name]).read_text())
    return list(data["captures"])


def starter() -> list[dict[str, Any]]:
    """The starter library (slots 101-120): see docs/nam.md."""
    return pack("starter")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._+'()-]+", "-", name).strip(" .-") or "capture"


def wav_name(slot: int, name: str) -> str:
    """HX Edit names an IR after its file and keeps 16 characters, so keep the slot in them."""
    return _safe(f"{slot} {name}")[:16].rstrip(" .-") + ".wav"


def add(ref: str | int, *, name: str = "", slot: int | None = None, description: str = "",
        model_hint: str = "", cab: bool | None = None, client: tone3000.Client | None = None,
        convert: Callable[[Path, Path], str] | None = None,
        report: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Download one capture from a TONE3000 tone (link or id), convert it, and list it."""
    if slot is not None and not 1 <= slot <= xl.IR_SLOTS:
        raise ValueError(f"IR slots run 1 to {xl.IR_SLOTS}")
    client = client or tone3000.Client()
    tid = tone3000.tone_id(ref)
    tone = client.tone(tid)
    model = tone3000.pick_model(client.models(tid), model_hint)
    name = (name or str(tone.get("title") or f"Tone {tid}")).strip()[:40]
    report(f"{name}: downloading \"{model.get('name')}\" from {tone.get('title')}")
    nam_path = nam_dir() / f"{_safe(name)}.nam"
    nam_path.parent.mkdir(parents=True, exist_ok=True)
    nam_path.write_bytes(client.download(model))

    wav_path = None
    if slot is not None:
        wav_path = ir_dir() / wav_name(slot, name)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        for old in ir_dir().glob(f"{slot} *.wav"):  # a slot holds one capture
            if old != wav_path:
                old.unlink()
        (convert or nam2ir.convert)(nam_path, wav_path)
        report(f"{name}: converted for IR slot {slot}")

    capture = {
        "name": name, "slot": slot,
        "description": description or _auto_description(tone, model),
        "cab": tone3000.describe(tone)["full_rig"] if cab is None else cab,
        "tone_id": tid, "model": str(model.get("name") or ""),
        "nam_file": str(nam_path), "ir_file": str(wav_path) if wav_path else None,
    }
    _list(capture)
    return capture


def add_many(entries: list[dict[str, Any]], *, client: tone3000.Client | None = None,
             convert: Callable[[Path, Path], str] | None = None,
             report: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Add each entry; one that fails is reported and skipped, not fatal."""
    client = client or tone3000.Client()
    added, failed = [], []
    for entry in entries:
        try:
            added.append(add(entry["tone"], name=entry.get("name", ""), slot=entry.get("slot"),
                             description=entry.get("description", ""),
                             model_hint=entry.get("model", ""), cab=entry.get("cab"),
                             client=client, convert=convert, report=report))
        except tone3000.NotSignedIn:
            raise
        except (tone3000.Tone3000Error, nam2ir.ConvertError, OSError, ValueError) as exc:
            failed.append({"name": entry.get("name") or str(entry.get("tone")), "error": str(exc)})
            report(f"Skipped {entry.get('name') or entry.get('tone')}: {exc}")
    return {"added": added, "failed": failed, "folder": str(folder()), "ir_folder": str(ir_dir())}


def _auto_description(tone: dict[str, Any], model: dict[str, Any]) -> str:
    makes = ", ".join(m.get("name", "") for m in tone.get("makes") or [] if m.get("name"))
    return "; ".join(p for p in (makes or str(tone.get("title") or ""), str(model.get("name") or "")) if p)[:200]


def _list(capture: dict[str, Any]) -> None:
    """Put the capture in Settings > NAM captures, replacing one of the same name or slot."""
    current = xl.parse_captures(config.get_setting("nam_captures"))
    keep = [c for c in current if c["name"].lower() != capture["name"].lower()
            and (capture["slot"] is None or c["slot"] != capture["slot"])]
    keep.append(capture)
    keep.sort(key=lambda c: (c["slot"] is None, c["slot"] or 0, c["name"].lower()))
    config.write_settings({"nam_captures": xl.parse_captures(keep)})
