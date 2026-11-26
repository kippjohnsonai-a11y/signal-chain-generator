"""The local web app: a JSON API over the library, plus the single-page UI.

Generation takes a minute or so of Claude calls, so it runs as a background
job; the page polls ``/api/jobs/<id>`` for progress lines and the result.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import (
    APP_FULL_NAME,
    assistant,
    claude,
    config,
    costs,
    desk,
    diagnostics,
    edit,
    engine,
    explain,
    importer,
    lan,
    library_io,
    live,
    namlib,
    pedal,
    quick_picks,
    tone3000,
    usb_relay,
    users,
    view,
    xl,
)
from .access import TailnetLoginGate
from .db import Library

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _code_version() -> dict[str, Any]:
    """The commit this server is running, so the launcher can spot a stale one."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h%x09%cs"], cwd=repo, capture_output=True,
            text=True, timeout=5, check=True,
        ).stdout.strip()
        commit, date = out.split("\t")
    except (OSError, subprocess.SubprocessError, ValueError):
        # Installed as a package (pipx, uvx): no git checkout, so name the release.
        try:
            from importlib.metadata import version

            return {"commit": version("signal-chain-generator"), "date": ""}
        except Exception:  # noqa: BLE001 - a version label must never stop the server
            return {"commit": "unknown", "date": ""}
    return {"commit": commit, "date": date}


#: Read once at startup: a server keeps running the code it started with.
VERSION = _code_version()


class Options(BaseModel):
    wah: bool = False
    expression: bool = False
    looper: bool = False
    capo: bool = False
    #: NAM rigs: only captures that include their cab, so no cab block is needed.
    full_rig: bool = False
    #: An off drive before the amp that Stage mode's "Gain boost" switches on.
    stage_boost: bool = True


def _with_preset_name(version: dict[str, Any], name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The version's chain and preset carrying a new name for the pedal's display."""
    clean = "".join(ch for ch in name if 32 <= ord(ch) < 127).strip()[: xl.PRESET_NAME_LIMIT]
    if not clean:
        raise HTTPException(400, "The preset name needs at least one letter or number")
    chain = copy.deepcopy(version["chain"])
    preset = copy.deepcopy(version["preset"])
    chain.setdefault("meta", {})["name"] = clean
    preset.setdefault("data", {}).setdefault("meta", {})["name"] = clean
    return chain, preset


TONE_STARTERS = Path(__file__).parent / "tone_starters.json"


def tone_starters() -> dict[str, Any]:
    """Suggested starting tweaks and gear notes, offered in the "My tone" dialog."""
    try:
        return json.loads(TONE_STARTERS.read_text())
    except (OSError, ValueError):
        return {"tweaks": [], "gear_notes": []}


def _options(version: dict[str, Any]) -> dict[str, Any]:
    """A version's toggles, with the Poly Capo read off its chain: older versions
    predate the toggle, and a hand edit can add or remove the block."""
    blocks = (version.get("chain") or {}).get("blocks") or []
    capo = any((b.get("model") if isinstance(b, dict) else b) == xl.CAPO_MODEL for b in blocks)
    return {**version["options"], "capo": capo}


class GenerateIn(BaseModel):
    song: str
    guitar_id: int
    rig_id: int
    options: Options = Options()
    instructions: str = ""
    song_id: int | None = None


class BlankIn(BaseModel):
    title: str
    artist: str = ""
    guitar_id: int
    rig_id: int
    start: str = "amp"
    tempo: float = 120.0


class ImportHlxIn(BaseModel):
    hlx: dict[str, Any]
    title: str = ""
    artist: str = ""
    guitar_id: int
    rig_id: int


class ImportPedalIn(BaseModel):
    slot: int
    title: str = ""
    artist: str = ""
    guitar_id: int
    rig_id: int


class RefineIn(BaseModel):
    feedback: str
    remember: bool = False
    remember_for_guitar: bool = False


class ForkIn(BaseModel):
    guitar_id: int
    rig_id: int
    options: Options
    instructions: str = ""
    label: str = ""
    #: Keep the exact signal chain and only re-dial its settings (another guitar).
    keep_chain: bool = False


class GuitarIn(BaseModel):
    name: str
    pickups: str
    pickup_notes: str = ""
    tuning: str = "E standard"
    notes: str = ""


class PreferenceIn(BaseModel):
    text: str
    guitar_id: int | None = None


class ToggleIn(BaseModel):
    active: bool


class PreferenceUpdate(BaseModel):
    text: str | None = None
    guitar_id: int | None = None
    active: bool | None = None


GEAR_KINDS = ("nam", "model", "category")


class GearNoteIn(BaseModel):
    kind: str
    name: str
    text: str


class GearNoteUpdate(BaseModel):
    kind: str | None = None
    name: str | None = None
    text: str | None = None
    active: bool | None = None


class LabelIn(BaseModel):
    label: str
    #: The name the pedal shows (16 characters); unchanged when left out.
    preset_name: str | None = None


class DuplicateIn(BaseModel):
    label: str = ""
    preset_name: str | None = None


class NotesIn(BaseModel):
    text: str


class TagsIn(BaseModel):
    tags: list[str]


class BulkIn(BaseModel):
    ids: list[int]


class BulkTagsIn(BaseModel):
    ids: list[int]
    add: list[str] = []
    remove: list[str] = []


class FolderIn(BaseModel):
    name: str


class SongFolderIn(BaseModel):
    folder_id: int | None = None


class SongOrderIn(BaseModel):
    song_ids: list[int]


class SongSetsIn(BaseModel):
    set_ids: list[int]


class SetPushIn(BaseModel):
    start_slot: int


class StarIn(BaseModel):
    starred: bool


class EditIn(BaseModel):
    edits: list[dict[str, Any]]
    label: str = ""
    #: A quick tweak (values only): named "Rehearsal <date>" and starred, so
    #: the song opens on it next time.
    rehearsal: bool = False


class DeskOpenIn(BaseModel):
    slot: int
    #: Take the pedal for live edit straight away (what the pane does).
    live: bool = True


class DeskSaveIn(BaseModel):
    edits: list[dict[str, Any]] = []


class DeskSongIn(BaseModel):
    edits: list[dict[str, Any]] = []
    title: str = ""
    artist: str = ""
    guitar_id: int
    rig_id: int


class ChatTurn(BaseModel):
    role: str = "user"
    text: str = ""


class DeskChatIn(BaseModel):
    messages: list[ChatTurn]
    edits: list[dict[str, Any]] = []
    snapshot: int = 0
    guitar_id: int | None = None


class ExplainIn(BaseModel):
    block: int = 0
    #: "input" or "output" to explain those instead of a block.
    io: str | None = None
    #: Unsaved hand edits, so a block swapped in edit mode can be explained.
    edits: list[dict[str, Any]] = []
    refresh: bool = False


class PushIn(BaseModel):
    slot: int


class QuickPickIn(BaseModel):
    kind: str
    name: str
    label: str
    #: "Presence -1, LowCut = 100", or the parsed list.
    changes: str | list[dict[str, Any]]
    match: str = ""


class QuickPickUpdate(BaseModel):
    kind: str | None = None
    name: str | None = None
    label: str | None = None
    changes: str | list[dict[str, Any]] | None = None
    match: str | None = None
    position: float | None = None


class QuickPickRestore(BaseModel):
    category: str | None = None


class LiveStartIn(BaseModel):
    slot: int | None = None
    edits: list[dict[str, Any]] = []


class LiveStopIn(BaseModel):
    keep: bool = False
    #: The version a save made, which the slot now holds.
    version_id: int | None = None


class SettingsIn(BaseModel):
    anthropic_api_key: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    exp_pedal: str | None = None
    gate_mode: str | None = None
    default_slot: int | None = None
    nam_captures: list[dict[str, Any]] | None = None
    tone3000_key: str | None = None
    diagnostics_token: str | None = None


class LanIn(BaseModel):
    enabled: bool


class WriteTestIn(BaseModel):
    slot: int | None = None


class DiagnosticsIn(BaseModel):
    note: str = ""
    error: str = ""
    user_agent: str = ""
    relay: dict[str, Any] | None = None
    #: What this browser showed and sent lately, newest last (see app.js).
    events: list[str] = []


class NamAddIn(BaseModel):
    tone: str
    slot: int | None = None
    name: str = ""
    description: str = ""
    model: str = ""


class Libraries:
    """Each person's library: the owner's is the one the app was made with.

    Stands in for a :class:`Library` and hands every call to the library of
    whoever the request is for (see :mod:`stompxl.users`).
    """

    def __init__(self, owner: Library | None = None):
        self._owner = owner
        self._guests: dict[str, Library] = {}
        self._lock = threading.Lock()

    def mine(self) -> Library:
        with self._lock:
            if not users.is_guest():
                if self._owner is None:
                    self._owner = Library()
                return self._owner
            who = users.current()
            if who not in self._guests:
                self._guests[who] = Library(config.db_path())
            return self._guests[who]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.mine(), name)


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        self._lock = threading.Lock()

    def start(self, work: Any) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "status": "running", "log": [], "result": None, "error": None}
        with self._lock:
            self._jobs[job_id] = job
            self._owners[job_id] = users.current()

        def report(message: str) -> None:
            job["log"].append(message)

        def run() -> None:
            status = "error"
            with costs.meter() as spent:
                try:
                    job["result"] = work(report)
                    status = "done"
                except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                    logger.error("Job failed: %s", traceback.format_exc())
                    job["error"] = str(exc)
                    job["dsp_refused"] = getattr(exc, "dsp_refused", None)
                finally:
                    # Claude's share of the job, in dollars; None when it didn't call Claude.
                    # Set before the status, so whoever sees "done" sees the cost too.
                    job["cost_usd"] = round(sum(spent), 4) if spent else None
                    job["status"] = status

        users.spawn(run)
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None or self._owners.get(job_id) != users.current():
            raise HTTPException(404, "No such job")
        return job


def create_app(library: Library | None = None, caller_factory: Any = None) -> FastAPI:
    lib: Any = Libraries(library)
    jobs = Jobs()
    build_caller = caller_factory or (lambda: claude.build_caller())

    def make_caller() -> Any:
        users.charge_claude_job()
        return build_caller()
    app = FastAPI(title=APP_FULL_NAME)
    diagnostics.install_log_ring()

    @app.exception_handler(HTTPException)
    async def logged_http_error(request: Request, exc: HTTPException) -> Response:
        # Every error the app shows lands in the log too, so Send diagnostics
        # has it even when nobody tapped the button on that error.
        if exc.status_code >= 400 and exc.status_code != 404:
            logger.warning("%s %s -> %d: %s", request.method, request.url.path,
                           exc.status_code, exc.detail)
        return await http_exception_handler(request, exc)
    # Only checks anything when STOMPXL_ALLOWED_LOGINS is set (the Mac mini).
    app.add_middleware(TailnetLoginGate)

    def _need(obj: Any, what: str) -> Any:
        if obj is None:
            raise HTTPException(404, f"No such {what}")
        return obj

    def _record(version_id: int) -> dict[str, Any]:
        """A library version, or the preset open in the pedal pane (:data:`desk.DESK_ID`)."""
        if version_id == desk.DESK_ID:
            return _need(desk.record(), "preset open from the pedal (open it again)")
        return _need(lib.version(version_id), "version")

    def _setup(guitar_id: int, rig_id: int, options: Options, instructions: str) -> engine.Setup:
        guitar = _need(lib.guitar(guitar_id), "guitar")
        rig = _need(lib.rig(rig_id), "rig")
        prefs = [p["text"] for p in lib.preferences(guitar_id, active_only=True)]
        exp_pedal = config.get_setting("exp_pedal") or xl.DEFAULT_EXP_PEDAL
        gate_mode = config.get_setting("gate_mode") or xl.DEFAULT_GATE
        captures = xl.captures_for(rig["kind"], config.read_settings(), full_rig=options.full_rig)
        if options.full_rig and rig["kind"] == "nam_pedal" and not captures:
            raise HTTPException(400, "None of your NAM captures in an IR slot include their cab. "
                                     "Add the full-rig pack in Settings > Get captures from TONE3000, "
                                     "or turn off \"Full-rig captures only\".")
        return engine.Setup(guitar=guitar, rig=rig, options=options.model_dump(),
                            preferences=prefs, instructions=instructions, exp_pedal=exp_pedal,
                            gate_mode=gate_mode, captures=captures,
                            gear_notes=lib.gear_notes(active_only=True))

    def _save(result: engine.Result, *, song_id: int, parent: dict[str, Any] | None, kind: str,
              label: str, setup: engine.Setup, guitar_id: int, rig_id: int,
              feedback: str = "") -> dict[str, Any]:
        notes = result.notes
        if parent is not None:
            changes = engine.diff_chains(parent["chain"], result.chain)
            if changes:
                notes += "\n\nChanges:\n" + "\n".join(f"- {c}" for c in changes)
        version_id = lib.add_version(
            song_id=song_id, parent_id=parent["id"] if parent else None, kind=kind, label=label,
            guitar_id=guitar_id, rig_id=rig_id, options=setup.options, brief=result.brief,
            chain=result.chain, preset=result.preset, feedback=feedback, notes=notes,
            warnings=result.warnings,
        )
        return {"song_id": song_id, "version_id": version_id}

    # -- state ---------------------------------------------------------------

    @app.get("/api/version")
    def version_info() -> dict[str, Any]:
        return VERSION

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        settings = config.read_settings()
        return {
            "guitars": lib.guitars(),
            "rigs": lib.rigs(),
            "preferences": lib.all_preferences(),
            "gear_notes": lib.gear_notes(),
            "quick_picks": lib.quick_picks(),
            "quick_main": quick_picks.main_controls(),
            "tone_starters": tone_starters(),
            "songs": lib.songs(),
            "folders": lib.folders(),
            "set_songs": lib.set_songs(),
            "tags": lib.tags(),
            "settings": {
                "has_api_key": bool(config.anthropic_api_key()),
                "claude_cost_month": round(costs.month_total(), 2),
                "claude_model": settings.get("claude_model") or claude.DEFAULT_MODEL,
                "claude_effort": settings.get("claude_effort") or claude.DEFAULT_EFFORT,
                "default_slot": settings.get("default_slot", 31),
                "efforts": list(claude.EFFORTS),
                "exp_pedal": settings.get("exp_pedal") or xl.DEFAULT_EXP_PEDAL,
                "exp_pedals": xl.EXP_PEDAL_SETUPS,
                "gate_mode": settings.get("gate_mode") or xl.DEFAULT_GATE,
                "gate_modes": xl.GATE_MODES,
                "nam_captures": xl.parse_captures(settings.get("nam_captures")),
                "nam_usb_routing": xl.usb_routing(settings) is not None,
                "tone3000_key": tone3000.has_key(),
                "tone3000_signed_in": tone3000.signed_in(),
                "diagnostics_token": bool(diagnostics.token()),
                "data_dir": str(config.data_dir()),
            },
            "account": {
                "email": users.current(),
                "guest": users.is_guest(),
                "claude_left": (max(0, users.daily_allowance() - users.claude_used_today())
                                if users.is_guest() else None),
            },
            "slot_count": xl.SLOT_COUNT,
            "version": VERSION,
        }

    @app.get("/api/claude-models")
    def claude_models(refresh: bool = False) -> dict[str, Any]:
        """The models the API key can use, newest first, for the Settings dropdown."""
        try:
            models = claude.list_models(refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - no key yet, or no network: say so in the dropdown
            return {"models": [], "latest_opus": claude.FALLBACK_MODEL, "error": str(exc)}
        return {"models": models, "latest_opus": claude.latest_opus(models), "error": None}

    @app.get("/api/dsp-room")
    def dsp_room(rig_id: int, wah: bool = False, expression: bool = False,
                 looper: bool = False, capo: bool = False, full_rig: bool = False) -> dict[str, Any]:
        """How much DSP the rig and toggles leave for the rest of the chain."""
        rig = _need(lib.rig(rig_id), "rig")
        opts = {"wah": wah, "expression": expression, "looper": looper, "capo": capo,
                "full_rig": full_rig}
        return xl.dsp_room(rig["kind"], opts, config.read_settings())

    @app.put("/api/settings")
    def put_settings(body: SettingsIn) -> dict[str, Any]:
        values = body.model_dump(exclude_unset=True)
        if users.is_guest():
            # Guests run on the owner's keys; they never see or replace them.
            values.pop("anthropic_api_key", None)
            values.pop("tone3000_key", None)
            values.pop("diagnostics_token", None)
        if "anthropic_api_key" in values:
            values["anthropic_api_key"] = (values["anthropic_api_key"] or "").strip() or None
            claude.forget_models()
        if "diagnostics_token" in values:
            values["diagnostics_token"] = (values["diagnostics_token"] or "").strip() or None
        if values.get("exp_pedal") and values["exp_pedal"] not in xl.EXP_PEDAL_SETUPS:
            raise HTTPException(400, "Unknown expression pedal setup")
        if "tone3000_key" in values:
            key = (values["tone3000_key"] or "").strip()
            if key and not key.startswith("t3k_pub_"):
                raise HTTPException(400, "Use the publishable key (t3k_pub_...), never the secret one")
            values["tone3000_key"] = key or None
        if values.get("gate_mode") and values["gate_mode"] not in xl.GATE_MODES:
            raise HTTPException(400, "Unknown noise gate setting")
        if "nam_captures" in values:
            # The Settings text box has no room for where a capture came from: keep it by name.
            known = {c["name"].lower(): c for c in xl.parse_captures(config.get_setting("nam_captures"))}
            values["nam_captures"] = xl.parse_captures([
                {**{k: v for k, v in known.get(str(c.get("name") or "").strip().lower(), {}).items()
                    if k in xl.CAPTURE_SOURCE_KEYS}, **c}
                for c in values["nam_captures"] if isinstance(c, dict)])
        config.write_settings(values)
        return {"ok": True}

    # -- phones on this Wi-Fi (see lan.py) ---------------------------------------

    def _lan_admin() -> None:
        if users.is_guest() or lan.is_phone():
            raise HTTPException(403, "Pairing is only on the computer that runs the app.")

    def _lan_status() -> dict[str, Any]:
        return {"enabled": lan.enabled(), "listening": lan.listening(), "port": lan.port(),
                "ip": lan.local_ip(), "devices": lan.public_devices()}

    @app.get("/api/lan")
    def lan_status() -> dict[str, Any]:
        _lan_admin()
        return _lan_status()

    @app.put("/api/lan")
    def lan_switch(body: LanIn) -> dict[str, Any]:
        _lan_admin()
        config.write_settings({"lan_enabled": body.enabled or None})
        if body.enabled:
            lan.start(app)
            for _ in range(20):  # give it a moment to bind, so the page can say if it didn't
                if lan.listening():
                    break
                time.sleep(0.1)
        else:
            lan.stop()
        return _lan_status()

    @app.post("/api/lan/pair")
    def lan_pair() -> dict[str, Any]:
        _lan_admin()
        if not lan.listening():
            raise HTTPException(400, "Turn on Phones on this Wi-Fi first.")
        code = lan.new_code()
        url = lan.pair_url(code)
        if url is None:
            raise HTTPException(400, "This computer doesn't seem to be on a network.")
        return {"code": code, "url": url, "qr_svg": lan.qr_svg(url),
                "expires_in": lan.CODE_SECONDS}

    @app.delete("/api/lan/devices/{device_id}")
    def lan_forget(device_id: str) -> dict[str, Any]:
        _lan_admin()
        lan.forget(device_id)
        return _lan_status()

    # -- first-run setup ---------------------------------------------------------

    @app.get("/api/setup")
    def setup_status() -> dict[str, Any]:
        """What a new install still needs: shown once, until it's done or dismissed."""
        guest = users.is_guest()
        hx_edit = None
        if not guest:
            from stompxl.helixgen.device.hxedit import find_hx_edit

            found = find_hx_edit()
            hx_edit = str(found) if found else None
        return {
            "guest": guest,
            "api_key": bool(config.anthropic_api_key()),
            "guitars": len(lib.guitars()),
            "hx_edit": hx_edit,
            "done": bool(config.get_setting("setup_done")),
        }

    @app.post("/api/setup/done")
    def setup_done() -> dict[str, Any]:
        config.write_settings({"setup_done": True})
        return {"ok": True}

    # -- guitars and preferences -----------------------------------------------

    @app.post("/api/guitars")
    def add_guitar(body: GuitarIn) -> dict[str, Any]:
        return {"id": lib.add_guitar(**body.model_dump())}

    @app.put("/api/guitars/{guitar_id}")
    def update_guitar(guitar_id: int, body: GuitarIn) -> dict[str, Any]:
        _need(lib.guitar(guitar_id), "guitar")
        lib.update_guitar(guitar_id, **body.model_dump())
        return {"ok": True}

    @app.delete("/api/guitars/{guitar_id}")
    def delete_guitar(guitar_id: int) -> dict[str, Any]:
        lib.delete_guitar(guitar_id)
        return {"ok": True}

    @app.post("/api/preferences")
    def add_preference(body: PreferenceIn) -> dict[str, Any]:
        return {"id": lib.add_preference(body.text.strip(), body.guitar_id)}

    @app.put("/api/preferences/{pref_id}")
    def update_preference(pref_id: int, body: PreferenceUpdate) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        if "text" in fields:
            fields["text"] = (fields["text"] or "").strip()
            if not fields["text"]:
                raise HTTPException(400, "A tweak needs some text.")
        if fields.get("active") is None:
            fields.pop("active", None)
        lib.update_preference(pref_id, fields)
        return {"ok": True}

    @app.delete("/api/preferences/{pref_id}")
    def delete_preference(pref_id: int) -> dict[str, Any]:
        lib.delete_preference(pref_id)
        return {"ok": True}

    # -- gear notes ----------------------------------------------------------

    def _gear_fields(fields: dict[str, Any]) -> dict[str, Any]:
        for key in ("name", "text"):
            if key in fields:
                fields[key] = " ".join(str(fields[key] or "").split()) if key == "name" \
                    else str(fields[key] or "").strip()
                if not fields[key]:
                    raise HTTPException(400, f"A gear note needs a {'gear name' if key == 'name' else 'note'}.")
        if "kind" in fields and fields["kind"] not in GEAR_KINDS:
            raise HTTPException(400, "Unknown kind of gear")
        if fields.get("active", True) is None:
            fields.pop("active")
        return fields

    @app.post("/api/gear-notes")
    def add_gear_note(body: GearNoteIn) -> dict[str, Any]:
        f = _gear_fields(body.model_dump())
        return {"id": lib.add_gear_note(f["kind"], f["name"], f["text"])}

    @app.put("/api/gear-notes/{note_id}")
    def update_gear_note(note_id: int, body: GearNoteUpdate) -> dict[str, Any]:
        _need(lib.gear_note(note_id), "gear note")
        lib.update_gear_note(note_id, _gear_fields(body.model_dump(exclude_unset=True)))
        return {"ok": True}

    @app.delete("/api/gear-notes/{note_id}")
    def delete_gear_note(note_id: int) -> dict[str, Any]:
        lib.delete_gear_note(note_id)
        return {"ok": True}

    # -- generation --------------------------------------------------------------

    # -- quick picks (the pedal close-up's one-tap moves) ----------------------

    def _pick_fields(fields: dict[str, Any]) -> dict[str, Any]:
        try:
            return quick_picks.clean(fields)
        except quick_picks.PickError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/quick-picks")
    def add_quick_pick(body: QuickPickIn) -> dict[str, Any]:
        f = _pick_fields(body.model_dump())
        pick_id = lib.add_quick_pick(f["kind"], f["name"], f["label"], f["changes"],
                                     f.get("match", ""))
        return {"id": pick_id, "quick_picks": lib.quick_picks()}

    @app.put("/api/quick-picks/{pick_id}")
    def update_quick_pick(pick_id: int, body: QuickPickUpdate) -> dict[str, Any]:
        _need(lib.quick_pick(pick_id), "quick pick")
        fields = body.model_dump(exclude_none=True)
        position = fields.pop("position", None)
        cleaned = _pick_fields(fields)
        if position is not None:
            cleaned["position"] = position
        lib.update_quick_pick(pick_id, cleaned)
        return {"quick_picks": lib.quick_picks()}

    @app.delete("/api/quick-picks/{pick_id}")
    def delete_quick_pick(pick_id: int) -> dict[str, Any]:
        lib.delete_quick_pick(pick_id)
        return {"quick_picks": lib.quick_picks()}

    @app.post("/api/quick-picks/restore")
    def restore_quick_picks(body: QuickPickRestore) -> dict[str, Any]:
        lib.restore_quick_picks(body.category)
        return {"quick_picks": lib.quick_picks()}

    @app.post("/api/generate")
    def generate(body: GenerateIn) -> dict[str, Any]:
        setup = _setup(body.guitar_id, body.rig_id, body.options, body.instructions)

        def work(report: Any) -> dict[str, Any]:
            result = engine.generate(body.song, setup, make_caller(), report)
            song_id = body.song_id
            if song_id is None:
                song_id = lib.add_song(body.song, result.brief.get("title") or body.song,
                                       result.brief.get("artist") or "")
            return _save(result, song_id=song_id, parent=None, kind="generate",
                         label=f"{setup.guitar['name']} - {setup.rig['name']}", setup=setup,
                         guitar_id=body.guitar_id, rig_id=body.rig_id)

        return {"job_id": jobs.start(work)}

    def _new_song_version(*, title: str, artist: str, chain: dict[str, Any], kind: str,
                          label: str, guitar_id: int, rig_id: int, notes: str,
                          origin: str) -> dict[str, Any]:
        _need(lib.guitar(guitar_id), "guitar")
        _need(lib.rig(rig_id), "rig")
        try:
            preset, warnings = engine.build_preset(chain)
        except Exception as exc:
            raise HTTPException(400, f"That preset didn't build: {exc}") from exc
        tempo = float(chain.get("global", {}).get("@tempo") or 120.0)
        brief = importer.stub_brief(title, artist, tempo,
                                    [s.get("name", "") for s in chain.get("snapshots", [])],
                                    chain["meta"]["name"], origin=origin)
        query = f"{title} by {artist}" if artist else title
        song_id = lib.add_song(query, title, artist)
        blocks = [b["model"] for b in chain.get("blocks", [])]
        options = {"wah": any(engine.catalog().get(m).category == xl.WAH_CATEGORY for m in blocks),
                   "expression": any(b.get("expression") for b in chain.get("blocks", [])),
                   "looper": xl.LOOPER_MODEL in blocks, "capo": xl.CAPO_MODEL in blocks}
        version_id = lib.add_version(
            song_id=song_id, parent_id=None, kind=kind, label=label, guitar_id=guitar_id,
            rig_id=rig_id, options=options, brief=brief, chain=chain, preset=preset,
            notes=notes, warnings=warnings,
        )
        return {"song_id": song_id, "version_id": version_id}

    @app.post("/api/songs/blank")
    def blank_song(body: BlankIn) -> dict[str, Any]:
        """A song with a blank preset to build by hand: no Claude call."""
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "Give the song a title")
        rig = _need(lib.rig(body.rig_id), "rig")
        if body.start not in importer.STARTS:
            raise HTTPException(400, f"Start must be one of {', '.join(importer.STARTS)}")
        name = title[: xl.PRESET_NAME_LIMIT]
        chain = importer.blank_chain(name, rig["kind"], body.start,
                                     config.get_setting("gate_mode") or xl.DEFAULT_GATE, body.tempo)
        start = ("a clean US Deluxe amp and its cab" if chain["blocks"] else "no blocks")
        return _new_song_version(
            title=title, artist=body.artist.strip(), chain=chain, kind="blank", label="Blank",
            guitar_id=body.guitar_id, rig_id=body.rig_id, origin="blank",
            notes=f"Started blank with {start}. Click Edit values to build it.")

    def _import(hlx: dict[str, Any], *, source: str, title: str, artist: str,
                guitar_id: int, rig_id: int) -> dict[str, Any]:
        try:
            chain, notes = importer.hlx_to_chain(
                hlx, captures=xl.parse_captures(config.get_setting("nam_captures")))
        except importer.ImportError_ as exc:
            raise HTTPException(400, str(exc)) from exc
        original = chain["meta"]["name"]
        if title.strip():
            # The title you give becomes the name the pedal shows, too.
            name = "".join(ch for ch in title if 32 <= ord(ch) < 127).strip()[: xl.PRESET_NAME_LIMIT]
            if name:
                chain["meta"]["name"] = chain["title"] = name
        lines = [f"Imported from {source} ({len(chain['blocks'])} blocks)"
                 + (f", originally named \"{original}\"." if chain["meta"]["name"] != original else ".")]
        lines += [f"- {n}" for n in notes]
        if warning := edit.dsp_warning(chain):
            lines.append(f"- {warning}")
        return _new_song_version(
            title=title.strip() or chain["meta"]["name"], artist=artist.strip(), chain=chain,
            kind="import", label="Imported", guitar_id=guitar_id, rig_id=rig_id,
            origin="import", notes="\n".join(lines))

    @app.post("/api/import/hlx")
    def import_hlx(body: ImportHlxIn) -> dict[str, Any]:
        """A song from an .hlx file (exported from HX Edit, or downloaded): no Claude call."""
        return _import(body.hlx, source="an .hlx file", title=body.title, artist=body.artist,
                       guitar_id=body.guitar_id, rig_id=body.rig_id)

    @app.post("/api/import/pedal")
    def import_pedal(body: ImportPedalIn) -> dict[str, Any]:
        """A song from a preset read off the pedal over USB: read-only, no Claude call."""
        if not 0 <= body.slot < xl.SLOT_COUNT:
            raise HTTPException(400, "Slot out of range")
        _need(lib.guitar(body.guitar_id), "guitar")
        _need(lib.rig(body.rig_id), "rig")

        def work(report: Any) -> dict[str, Any]:
            report(f"Reading slot {pedal.slot_label(body.slot)} from the pedal...")
            hlx = pedal.read_preset(body.slot)
            report("Converting...")
            try:
                return _import(hlx, source=f"pedal slot {pedal.slot_label(body.slot)}",
                               title=body.title, artist=body.artist,
                               guitar_id=body.guitar_id, rig_id=body.rig_id)
            except HTTPException as exc:
                raise RuntimeError(exc.detail) from exc

        return {"job_id": jobs.start(work)}

    @app.post("/api/versions/{version_id}/plan")
    def write_plan(version_id: int) -> dict[str, Any]:
        """Have Claude write the song plan for a blank or imported version. The
        preset itself is left exactly as it is."""
        record = _need(lib.version(version_id), "version")
        song = _need(lib.song(record["song_id"]), "song")
        setup = _setup(record["guitar_id"], record["rig_id"], Options(**_options(record)), "")

        def work(report: Any) -> dict[str, Any]:
            report("Asking Claude about the song...")
            brief = engine.brief_only(song["query"], setup, make_caller(), report)
            brief["preset_name"] = record["brief"].get("preset_name", brief.get("preset_name"))
            lib.set_brief(version_id, brief)
            return {"song_id": record["song_id"], "version_id": version_id}

        return {"job_id": jobs.start(work)}

    @app.post("/api/versions/{version_id}/refine")
    def refine(version_id: int, body: RefineIn) -> dict[str, Any]:
        parent = _need(lib.version(version_id), "version")
        if not body.feedback.strip():
            raise HTTPException(400, "Say what to change.")
        if body.remember:
            lib.add_preference(body.feedback.strip(),
                               parent["guitar_id"] if body.remember_for_guitar else None)
        opts = Options(**_options(parent))
        setup = _setup(parent["guitar_id"], parent["rig_id"], opts, "")
        song = lib.song(parent["song_id"])
        history = lib.lineage_feedback(version_id)
        feedback = body.feedback.strip()
        if history:
            feedback += "\n(Earlier feedback on this preset, already applied: " + "; ".join(history) + ")"

        def work(report: Any) -> dict[str, Any]:
            result = engine.refine(song["query"], setup, make_caller(), parent["chain"],
                                   parent["brief"], feedback, report)
            return _save(result, song_id=parent["song_id"], parent=parent, kind="refine",
                         label=_short(body.feedback), setup=setup,
                         guitar_id=parent["guitar_id"], rig_id=parent["rig_id"],
                         feedback=body.feedback.strip())

        return {"job_id": jobs.start(work)}

    @app.post("/api/versions/{version_id}/fork")
    def fork(version_id: int, body: ForkIn) -> dict[str, Any]:
        parent = _need(lib.version(version_id), "version")
        setup = _setup(body.guitar_id, body.rig_id, body.options, body.instructions)
        old_guitar = lib.guitar(parent["guitar_id"]) if parent["guitar_id"] else None
        old_rig = lib.rig(parent["rig_id"]) if parent["rig_id"] else None
        changes = []
        if body.guitar_id != parent["guitar_id"]:
            changes.append(
                f"Re-dial for the {setup.guitar['name']} ({setup.guitar['pickups']})"
                + (f" instead of the {old_guitar['name']} ({old_guitar['pickups']})" if old_guitar else "")
            )
        chain_changes = []
        if body.rig_id != parent["rig_id"]:
            chain_changes.append(f"Adapt for the '{setup.rig['name']}' rig"
                                 + (f" instead of '{old_rig['name']}'" if old_rig else ""))
        for name, on in body.options.model_dump().items():
            if bool(_options(parent).get(name)) != on:
                chain_changes.append(f"Turn the {name} {'on' if on else 'off'}")
        if body.keep_chain and chain_changes:
            raise HTTPException(400, "Keeping the same chain only works when just the guitar "
                                     "changes: put the rig and toggles back, or untick "
                                     "\"Keep the same signal chain\".")
        changes += chain_changes
        if body.keep_chain and changes and body.guitar_id != parent["guitar_id"]:
            changes.append("Keep every block; re-dial gain, EQ and levels for this guitar's "
                           "pickups (and its tuning, if a Poly Capo is on). Keep the snapshots "
                           "switching blocks on and off, sharing one set of values")
        if body.instructions.strip():
            changes.append(body.instructions.strip())
        song = lib.song(parent["song_id"])
        label = body.label or (f"{setup.guitar['name']} version" if body.keep_chain
                               else f"Fork: {setup.guitar['name']} - {setup.rig['name']}")

        if not changes:
            new_id = lib.add_version(
                song_id=parent["song_id"], parent_id=parent["id"], kind="fork", label=label,
                guitar_id=parent["guitar_id"], rig_id=parent["rig_id"], options=parent["options"],
                brief=parent["brief"], chain=parent["chain"], preset=parent["preset"],
                notes="Copied unchanged.", warnings=parent["warnings"],
            )
            return {"version_id": new_id, "song_id": parent["song_id"]}

        feedback = ". ".join(changes) + "."

        rebuild = engine.retune if body.keep_chain else engine.refine

        def work(report: Any) -> dict[str, Any]:
            result = rebuild(song["query"], setup, make_caller(), parent["chain"],
                             parent["brief"], feedback, report)
            return _save(result, song_id=parent["song_id"], parent=parent, kind="fork",
                         label=label, setup=setup, guitar_id=body.guitar_id,
                         rig_id=body.rig_id, feedback=feedback)

        return {"job_id": jobs.start(work)}

    def _edited(parent: dict[str, Any], edits: list[dict[str, Any]]
                ) -> tuple[dict[str, Any], list[str], dict[str, Any], list[str]]:
        if not edits:
            raise HTTPException(400, "Nothing changed")
        try:
            chain, changes = edit.apply_edits(parent["chain"], parent["preset"], edits)
            preset, warnings = engine.build_preset(chain)
        except edit.EditError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(400, f"The edited preset didn't build: {exc}") from exc
        if note := edit.dsp_warning(chain):
            changes.append(note)
        return chain, changes, preset, warnings

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        """Every model a block can be swapped to, for the block editor."""
        return {"models": [
            {"name": m.display_name, "category": m.category or "Other",
             "based_on": m.based_on or "", "dsp": round(xl.dsp_cost(m)),
             "params": [{"param": n, "name": view.label(n, m.get_parameter(n))}
                        for n in sorted(m.parameter_names())
                        if not n.startswith("@")
                        and m.get_parameter(n).controller_range() is not None]}
            for m in engine.catalog().models()
        ]}

    @app.post("/api/versions/{version_id}/preview")
    def preview_edit(version_id: int, body: EditIn) -> dict[str, Any]:
        """What the edits would make, without saving: for showing block changes live."""
        parent = _record(version_id)
        chain, changes, preset, _ = _edited(parent, body.edits)
        return {"view": view.preset_view(chain, preset), "chain": chain, "changes": changes}

    @app.post("/api/versions/{version_id}/edit")
    def hand_edit(version_id: int, body: EditIn) -> dict[str, Any]:
        """Save hand edits as a new version; no Claude call."""
        parent = _need(lib.version(version_id), "version")
        chain, changes, preset, warnings = _edited(parent, body.edits)
        label = body.label.strip()
        if not label and body.rehearsal:
            label = _rehearsal_label(parent["song_id"])
        new_id = lib.add_version(
            song_id=parent["song_id"], parent_id=parent["id"], kind="manual",
            label=label or f"Hand edit of v{parent['id']}",
            guitar_id=parent["guitar_id"], rig_id=parent["rig_id"], options=parent["options"],
            brief=parent["brief"], chain=chain, preset=preset,
            notes="Edited by hand:\n" + "\n".join(f"- {c}" for c in changes),
            warnings=warnings,
        )
        if body.rehearsal:
            lib.set_favorite(parent["song_id"], new_id)
        return {"version_id": new_id, "song_id": parent["song_id"], "changes": changes}

    def _rehearsal_label(song_id: int) -> str:
        """"Rehearsal Nov 15", then "Rehearsal Nov 15 (2)" for the next one that day."""
        now = time.localtime()
        base = f"Rehearsal {time.strftime('%b', now)} {now.tm_mday}"
        taken = {v["label"] for v in lib.versions(song_id)}
        n = 1
        label = base
        while label in taken:
            n += 1
            label = f"{base} ({n})"
        return label

    @app.post("/api/versions/{version_id}/explain")
    def explain_block(version_id: int, body: ExplainIn) -> dict[str, Any]:
        """A plain-English guide to one block's settings; cached once asked."""
        record = _record(version_id)
        chain, preset = record["chain"], record["preset"]
        if body.edits:
            chain, _, preset, _ = _edited(record, body.edits)
        shown = view.preset_view(chain, preset)
        if body.io:
            block = next((b for b in shown["io"] if b["io"] == body.io), None)
            if block is None:
                raise HTTPException(400, f"No {body.io}")
        elif 0 <= body.block < len(shown["blocks"]):
            block = shown["blocks"][body.block]
        else:
            raise HTTPException(400, f"No block {body.block}")
        # The pedal pane's id stands for whatever preset is open, so its key
        # carries the chain too.
        seed = [body.edits, record["chain"] if version_id == desk.DESK_ID else None]
        digest = hashlib.sha1(json.dumps(seed, sort_keys=True).encode()).hexdigest()[:12]
        key = f"{version_id}:{body.io or body.block}:{block['model']}:{digest}"
        if not body.refresh and (cached := lib.explanation(key)):
            return {"explanation": cached}

        song = lib.song(record["song_id"]) or {}
        guitar = lib.guitar(record["guitar_id"]) if record["guitar_id"] else None
        rig = lib.rig(record["rig_id"]) if record["rig_id"] else None
        context = {
            "song": f"{song.get('title', '?')} by {song.get('artist', '?')}",
            "guitar": f"{guitar['name']} ({guitar['pickups']})" if guitar else "?",
            "rig": rig["name"] if rig else "?",
        }

        def work(report: Any) -> dict[str, Any]:
            report(f"Asking Claude about the {block['model']}...")
            result = explain.explain(block, shown["snapshots"], context, make_caller())
            lib.save_explanation(key, result)
            return {"explanation": result}

        return {"job_id": jobs.start(work)}

    # -- NAM captures from TONE3000 ---------------------------------------------

    def _t3k(call: Any) -> Any:
        try:
            return call()
        except tone3000.NotSignedIn as exc:
            raise HTTPException(401, str(exc)) from exc
        except tone3000.Tone3000Error as exc:
            raise HTTPException(502, str(exc)) from exc

    def _owner_only(what: str) -> None:
        if users.is_guest():
            raise HTTPException(403, f"{what} is the app owner's: guests share their TONE3000 "
                                     "sign-in.")

    @app.get("/api/tone3000/signin")
    def tone3000_signin(request: Request) -> dict[str, Any]:
        _owner_only("Signing in to TONE3000")
        redirect = str(request.url_for("tone3000_callback"))
        return {"url": _t3k(lambda: tone3000.authorize_url(redirect))}

    @app.get("/api/tone3000/callback", response_class=HTMLResponse)
    def tone3000_callback(request: Request) -> HTMLResponse:
        _owner_only("Signing in to TONE3000")
        try:
            tone3000.finish_sign_in(dict(request.query_params))
            text, status = "Signed in to TONE3000. You can close this tab and go back to the app.", 200
        except tone3000.Tone3000Error as exc:
            text, status = f"TONE3000 sign-in didn't work: {exc}", 400
        page = f"<!doctype html><meta charset=utf-8><title>TONE3000</title><p>{_escape(text)}</p>"
        return HTMLResponse(page, status_code=status)

    @app.post("/api/tone3000/signout")
    def tone3000_signout() -> dict[str, Any]:
        _owner_only("Signing out of TONE3000")
        tone3000.sign_out()
        return {"ok": True}

    @app.get("/api/tone3000/search")
    def tone3000_search(q: str) -> dict[str, Any]:
        tones = _t3k(lambda: tone3000.Client().search(q))
        return {"tones": [tone3000.describe(t) for t in tones]}

    @app.post("/api/nam/add")
    def nam_add(body: NamAddIn) -> dict[str, Any]:
        _t3k(lambda: tone3000.tone_id(body.tone))
        entry = {"tone": body.tone, "slot": body.slot, "name": body.name,
                 "description": body.description, "model": body.model}
        return {"job_id": jobs.start(lambda report: namlib.add_many([entry], report=report))}

    @app.post("/api/nam/starter")
    def nam_starter() -> dict[str, Any]:
        return nam_pack("starter")

    @app.post("/api/nam/pack/{name}")
    def nam_pack(name: str) -> dict[str, Any]:
        try:
            entries = namlib.pack(name)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"job_id": jobs.start(lambda report: namlib.add_many(entries, report=report))}

    @app.get("/api/nam/download")
    def nam_download() -> Response:
        """The converted IRs (and the .nam files), zipped, for HX Edit on the player's computer."""
        files = [(path, path.name) for path in sorted(namlib.ir_dir().glob("*.wav"))]
        files += [(path, f"nam/{path.name}") for path in sorted(namlib.nam_dir().glob("*.nam"))]
        if not files:
            raise HTTPException(404, "No captures downloaded yet.")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, name in files:
                archive.write(path, name)
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="StompXL NAM IRs.zip"'})

    @app.post("/api/nam/open-folder")
    def nam_open_folder() -> dict[str, Any]:
        if users.is_guest():
            raise HTTPException(403, "The NAM folder is on the app's own computer, not yours.")
        path = namlib.ir_dir()
        path.mkdir(parents=True, exist_ok=True)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        with contextlib.suppress(OSError):
            subprocess.Popen([opener, str(path)])
        return {"folder": str(path)}

    @app.get("/api/library/export")
    def library_export() -> Response:
        """Everything in this person's library as one zip, keys left out."""
        tmp = Path(tempfile.mkdtemp(prefix="library-export-"))
        dest = tmp / "library.zip"
        library_io.export_zip(lib.mine(), dest)
        name = time.strftime("signal-chain-library-%Y-%m-%d.zip")
        return FileResponse(dest, media_type="application/zip", filename=name,
                            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))

    @app.post("/api/library/import")
    async def library_import(request: Request) -> dict[str, Any]:
        """Replace this person's library with an export; the current one is saved first."""
        with tempfile.TemporaryDirectory(prefix="library-import-") as tmp:
            source = Path(tmp) / "import.zip"
            size = 0
            with source.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > library_io.MAX_IMPORT_BYTES:
                        raise HTTPException(413, "That file is too big to be a library export.")
                    out.write(chunk)
            if not size:
                raise HTTPException(400, "No file came through. Pick the export again.")
            mine = lib.mine()
            try:
                result = await to_thread.run_sync(library_io.import_zip, mine, source)
            except library_io.ImportRefused as exc:
                raise HTTPException(400, str(exc)) from exc
        logger.info("Library imported: %s songs, %s files", result.get("songs"), result.get("files"))
        return result

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return jobs.get(job_id)

    # -- library -------------------------------------------------------------

    @app.get("/api/songs/{song_id}")
    def song(song_id: int) -> dict[str, Any]:
        record = _need(lib.song(song_id), "song")
        return {**record, "versions": lib.versions(song_id)}

    @app.delete("/api/songs/{song_id}")
    def delete_song(song_id: int) -> dict[str, Any]:
        lib.delete_song(song_id)
        return {"ok": True}

    @app.put("/api/songs/{song_id}/folder")
    def song_folder(song_id: int, body: SongFolderIn) -> dict[str, Any]:
        _need(lib.song(song_id), "song")
        if body.folder_id is not None and not any(f["id"] == body.folder_id for f in lib.folders()):
            raise HTTPException(404, "No such folder")
        lib.set_song_folder(song_id, body.folder_id)
        return {"ok": True}

    def _folder_name(body: FolderIn) -> str:
        name = " ".join(body.name.split())[:60]
        if not name:
            raise HTTPException(400, "The folder needs a name")
        return name

    @app.put("/api/folders/{folder_id}/order")
    def folder_order(folder_id: int, body: SongOrderIn) -> dict[str, Any]:
        """The folder's set list: songs in the order they're played."""
        if not any(f["id"] == folder_id for f in lib.folders()):
            raise HTTPException(404, "No such folder")
        lib.set_song_order(folder_id, body.song_ids)
        return {"ok": True}

    def _need_set(folder_id: int) -> None:
        if not any(f["id"] == folder_id for f in lib.folders()):
            raise HTTPException(404, "No such set")

    @app.put("/api/sets/{folder_id}/songs")
    def set_songs(folder_id: int, body: SongOrderIn) -> dict[str, Any]:
        """A set's songs in play order (songs left out leave the set)."""
        _need_set(folder_id)
        lib.replace_set(folder_id, body.song_ids)
        return {"ok": True}

    @app.put("/api/songs/{song_id}/sets")
    def song_sets(song_id: int, body: SongSetsIn) -> dict[str, Any]:
        _need(lib.song(song_id), "song")
        for fid in body.set_ids:
            _need_set(fid)
        lib.set_song_sets(song_id, body.set_ids)
        return {"ok": True}

    @app.post("/api/sets/{folder_id}/push")
    def push_set(folder_id: int, body: SetPushIn) -> dict[str, Any]:
        """Send a whole set to the pedal, one song per slot from start_slot, so the
        pedal's own preset switching follows the set. Each slot is backed up first."""
        _need_set(folder_id)
        songs = {s["id"]: s for s in lib.songs()}
        order = [songs[i] for i in lib.set_songs().get(folder_id, []) if i in songs]
        if not order:
            raise HTTPException(400, "This set has no songs yet")
        if body.start_slot < 0 or body.start_slot + len(order) > xl.SLOT_COUNT:
            raise HTTPException(400, f"{len(order)} songs from that slot run past the last slot")
        plan = []
        for n, song in enumerate(order):
            record = lib.version(song["open_version_id"]) if song["open_version_id"] else None
            if record is None:
                raise HTTPException(400, f"“{song['title']}” has no version to send")
            if (record["chain"].get("nam") or {}).get("mode") == "usb":
                raise HTTPException(400, f"“{song['title']}” is a NAM-on-computer preset: only HX Edit can load it")
            plan.append((body.start_slot + n, song, record))

        def work(report: Any) -> dict[str, Any]:
            for n, (slot, song, record) in enumerate(plan):
                report(f"{n + 1} of {len(plan)}: {song['title']} to slot {pedal.slot_label(slot)}...")
                pedal.push(record["preset"], slot, device_settings=record["chain"].get("device_settings"))
                lib.mark_pushed(record["id"], slot)
            report(f"Sent {len(plan)} songs to slots {pedal.slot_label(plan[0][0])} to {pedal.slot_label(plan[-1][0])}")
            return {"sent": len(plan)}

        return {"job_id": jobs.start(work)}

    @app.put("/api/songs/{song_id}/star")
    def star_song(song_id: int, body: StarIn) -> dict[str, Any]:
        _need(lib.song(song_id), "song")
        lib.set_starred(song_id, body.starred)
        return {"ok": True}

    @app.post("/api/songs/{song_id}/opened")
    def song_opened(song_id: int) -> dict[str, Any]:
        """Remember when a song was last opened, for the Recent list on every device."""
        _need(lib.song(song_id), "song")
        lib.mark_opened(song_id)
        return {"ok": True}

    @app.post("/api/folders")
    def add_folder(body: FolderIn) -> dict[str, Any]:
        return {"id": lib.add_folder(_folder_name(body))}

    @app.put("/api/folders/{folder_id}")
    def rename_folder(folder_id: int, body: FolderIn) -> dict[str, Any]:
        lib.rename_folder(folder_id, _folder_name(body))
        return {"ok": True}

    @app.delete("/api/folders/{folder_id}")
    def delete_folder(folder_id: int) -> dict[str, Any]:
        lib.delete_folder(folder_id)
        return {"ok": True}

    @app.put("/api/songs/{song_id}/favorite/{version_id}")
    def favorite(song_id: int, version_id: int) -> dict[str, Any]:
        lib.set_favorite(song_id, version_id)
        return {"ok": True}

    @app.get("/api/versions/{version_id}")
    def version(version_id: int) -> dict[str, Any]:
        record = _record(version_id)
        record["options"] = _options(record)
        record["preset_name"] = record["preset"].get("data", {}).get("meta", {}).get("name", "")
        record["view"] = view.preset_view(record["chain"], record["preset"])
        record.pop("preset")
        return record

    @app.put("/api/versions/{version_id}/label")
    def rename(version_id: int, body: LabelIn) -> dict[str, Any]:
        record = _need(lib.version(version_id), "version")
        lib.rename_version(version_id, body.label.strip())
        if body.preset_name is not None:
            chain, preset = _with_preset_name(record, body.preset_name)
            lib.set_preset_name(version_id, chain, preset)
        return {"ok": True}

    @app.post("/api/versions/{version_id}/duplicate")
    def duplicate(version_id: int, body: DuplicateIn) -> dict[str, Any]:
        """An exact copy under a new name: no Claude call, nothing re-dialled."""
        parent = _need(lib.version(version_id), "version")
        chain, preset = parent["chain"], parent["preset"]
        if body.preset_name is not None:
            chain, preset = _with_preset_name(parent, body.preset_name)
        new_id = lib.add_version(
            song_id=parent["song_id"], parent_id=parent["id"], kind="copy",
            label=body.label.strip() or f"Copy of {parent['label'] or 'v' + str(parent['id'])}",
            guitar_id=parent["guitar_id"], rig_id=parent["rig_id"], options=parent["options"],
            brief=parent["brief"], chain=chain, preset=preset, feedback="",
            notes=f"Copied from v{parent['id']}.\n\n{parent['notes']}".strip(),
            warnings=parent["warnings"],
        )
        return {"version_id": new_id, "song_id": parent["song_id"]}

    @app.put("/api/versions/{version_id}/notes")
    def user_notes(version_id: int, body: NotesIn) -> dict[str, Any]:
        _need(lib.version(version_id), "version")
        lib.set_user_notes(version_id, body.text)
        return {"ok": True}

    @app.put("/api/versions/{version_id}/tags")
    def version_tags(version_id: int, body: TagsIn) -> dict[str, Any]:
        _need(lib.version(version_id), "version")
        lib.set_tags(version_id, body.tags)
        return {"tags": lib.version(version_id)["tags"]}

    @app.post("/api/versions/bulk-tags")
    def bulk_tags(body: BulkTagsIn) -> dict[str, Any]:
        lib.tag_versions(body.ids, add=body.add, remove=body.remove)
        return {"ok": True}

    @app.post("/api/versions/bulk-delete")
    def bulk_delete(body: BulkIn) -> dict[str, Any]:
        return {"deleted": lib.delete_versions(body.ids)}

    @app.delete("/api/versions/{version_id}")
    def delete_version(version_id: int) -> dict[str, Any]:
        lib.delete_version(version_id)
        return {"ok": True}

    @app.get("/api/versions/{version_id}/hlx")
    def download(version_id: int) -> Response:
        record = _need(lib.version(version_id), "version")
        name = record["preset"]["data"]["meta"].get("name") or f"version-{version_id}"
        filename = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "preset"
        return Response(
            content=json.dumps(record["preset"], indent=2),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename} v{version_id}.hlx"'},
        )

    # -- pedal ---------------------------------------------------------------

    @app.get("/api/pedal")
    def pedal_status() -> dict[str, Any]:
        return pedal.status().__dict__

    @app.websocket("/api/usb-relay")
    async def usb_relay_socket(websocket: WebSocket) -> None:
        """The pedal plugged into the device this page runs on (WebUSB)."""
        await usb_relay.serve(websocket)

    def _slot_list(names: dict[int, str]) -> list[dict[str, Any]]:
        return [{"slot": s, "label": pedal.slot_label(s), "name": names.get(s, "")}
                for s in range(xl.SLOT_COUNT)]

    @app.get("/api/pedal/slots")
    def pedal_slots() -> dict[str, Any]:
        try:
            names = pedal.list_slots()
        except pedal.PedalError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"slots": _slot_list(names)}

    @app.post("/api/versions/{version_id}/push")
    def push(version_id: int, body: PushIn) -> dict[str, Any]:
        record = _need(lib.version(version_id), "version")
        if not 0 <= body.slot < xl.SLOT_COUNT:
            raise HTTPException(400, "Slot out of range")
        if (record["chain"].get("nam") or {}).get("mode") == "usb":
            # The USB push edits path A only; the sandwich needs path B, the
            # Send output and the USB input.
            raise HTTPException(
                400, "A NAM-on-computer preset uses both paths, which only HX Edit can load: "
                     "download the .hlx and drag it into HX Edit.")

        def work(report: Any) -> dict[str, Any]:
            report(f"Writing to slot {pedal.slot_label(body.slot)}...")
            summary = pedal.push(record["preset"], body.slot,
                                 device_settings=record["chain"].get("device_settings"))
            lib.mark_pushed(version_id, body.slot)
            config.write_settings({"default_slot": body.slot})
            report(summary)
            return {"version_id": version_id, "song_id": record["song_id"], "summary": summary}

        return {"job_id": jobs.start(work)}

    # -- diagnostics ---------------------------------------------------------

    def _diagnostics_body(body: DiagnosticsIn) -> str:
        if users.is_guest():
            raise HTTPException(403, "Diagnostics are the app owner's to send.")
        return diagnostics.bundle(VERSION, note=body.note[:2000], client={
            "error": body.error[:4000], "user_agent": body.user_agent[:300], "relay": body.relay,
            "events": [str(e)[:400] for e in body.events[-60:]]})

    @app.post("/api/diagnostics/preview")
    def diagnostics_preview(body: DiagnosticsIn) -> dict[str, Any]:
        """The bundle Send diagnostics would post, to read or copy by hand."""
        text = _diagnostics_body(body)
        title = f"Problem report: {diagnostics.headline(body.note, body.error)}"
        return {"text": text, "can_send": bool(diagnostics.token()), "repo": diagnostics.repo(),
                "issue_url": diagnostics.new_issue_url(title[:200])}

    @app.post("/api/diagnostics/send")
    def diagnostics_send(body: DiagnosticsIn) -> dict[str, Any]:
        """Post the bundle as an issue in the app's GitHub repository."""
        text = _diagnostics_body(body)
        title = f"Diagnostics {time.strftime('%Y-%m-%d %H:%M')}: {diagnostics.headline(body.note, body.error)}"
        try:
            url = diagnostics.send(title[:200], text)
        except diagnostics.SendError as exc:
            # Still hand the bundle back, so it can be copied instead.
            return {"ok": False, "error": str(exc), "text": text}
        logger.info("Diagnostics sent: %s", url)
        return {"ok": True, "url": url}

    @app.post("/api/diagnostics/write-test")
    def diagnostics_write_test(body: WriteTestIn) -> dict[str, Any]:
        """Write the send slot's own preset back into its edit buffer, unsaved."""
        if users.is_guest():
            raise HTTPException(403, "The pedal test is the app owner's to run.")
        slot = body.slot if body.slot is not None else config.get_setting("default_slot")
        slot = 0 if slot is None else slot
        if not 0 <= slot < xl.SLOT_COUNT:
            raise HTTPException(400, "Slot out of range")
        try:
            return pedal.write_test(slot)
        except pedal.PedalError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- live edit -----------------------------------------------------------

    def _live_preset(parent: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
        return _edited(parent, edits)[2] if edits else parent["preset"]

    @app.post("/api/versions/{version_id}/live")
    def live_start(version_id: int, body: LiveStartIn) -> dict[str, Any]:
        """Take the pedal and play this version's value edits on it as they happen."""
        record = _record(version_id)
        if (record["chain"].get("nam") or {}).get("mode") == "usb":
            raise HTTPException(400, "A NAM-on-computer preset can't be edited live: it lives "
                                     "in HX Edit.")
        slot = body.slot if body.slot is not None else record.get("pushed_slot")
        if slot is None:
            # Not sent from the app itself; the slot it last sent to is the best
            # guess, and the chain check below catches a wrong one.
            slot = config.get_setting("default_slot")
        if slot is None:
            raise HTTPException(400, "Send this version to the pedal first: live edit tunes the "
                                     "slot that holds it.")
        if not 0 <= slot < xl.SLOT_COUNT:
            raise HTTPException(400, "Slot out of range")
        preset = _live_preset(record, body.edits)
        try:
            live.start(version_id, record["preset"], slot)
            result = live.update(version_id, preset)
        except (live.LiveError, pedal.PedalError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            with contextlib.suppress(Exception):
                live.stop(keep=False)
            raise HTTPException(503, f"Couldn't open the pedal for live edit: {exc}") from exc
        return {"slot": slot, "label": pedal.slot_label(slot), **result}

    @app.post("/api/versions/{version_id}/live/update")
    def live_update(version_id: int, body: EditIn) -> dict[str, Any]:
        """Send the values these edits change to the pedal."""
        record = _record(version_id)
        preset = _live_preset(record, body.edits)
        try:
            return live.update(version_id, preset)
        except live.LiveEnded as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, f"Live edit stopped: {exc}") from exc

    @app.post("/api/live/stop")
    def live_stop(body: LiveStopIn) -> dict[str, Any]:
        """Let the pedal go, saving what it plays to the slot or putting the slot back."""
        try:
            slot = live.stop(keep=body.keep)
        except Exception as exc:
            raise HTTPException(503, f"Live edit ended, but the pedal didn't answer: {exc}") from exc
        if slot is not None and body.keep and body.version_id is not None:
            _need(lib.version(body.version_id), "version")
            lib.mark_pushed(body.version_id, slot)
        return {"slot": slot, "label": pedal.slot_label(slot) if slot is not None else ""}

    @app.get("/api/live")
    def live_status() -> dict[str, Any]:
        session = live.current()
        if session is None:
            return {"active": False}
        return {"active": True, "version_id": session.version_id, "slot": session.slot,
                "label": pedal.slot_label(session.slot)}

    # -- pedal pane ------------------------------------------------------------
    # Work on a preset straight off the pedal, without making it a song. The
    # open preset answers to version id desk.DESK_ID, so the chain view, the
    # close-up, live edit and Explain all work on it as on any version.

    def _desk_view() -> dict[str, Any]:
        _record(desk.DESK_ID)
        session = live.current()
        on = session is not None and session.version_id == desk.DESK_ID
        names = pedal.known_slots()
        return {"version": version(desk.DESK_ID),
                "live": {"slot": session.slot, "label": pedal.slot_label(session.slot)}
                if on else None,
                "slots": None if names is None else _slot_list(names)}

    def _desk_live_off() -> None:
        session = live.current()
        if session is not None and session.version_id == desk.DESK_ID:
            live.stop(keep=False)

    def _desk_live_on(record: dict[str, Any]) -> str:
        """Take the pedal for live edit on the open preset; "" or why it couldn't."""
        try:
            live.start(desk.DESK_ID, record["preset"], record["pushed_slot"])
        except (live.LiveError, pedal.PedalError) as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - shown in the pane, which still works
            return f"Couldn't open the pedal for live edit: {exc}"
        return ""

    @app.post("/api/desk/open")
    def desk_open(body: DeskOpenIn) -> dict[str, Any]:
        """Load a pedal slot into the pane (the pedal switches to it once live)."""
        if not 0 <= body.slot < xl.SLOT_COUNT:
            raise HTTPException(400, "Slot out of range")
        try:
            _desk_live_off()
        except Exception as exc:  # noqa: BLE001 - the old slot's edits are lost either way
            logger.info("Pedal pane: letting go of the last preset failed: %s", exc)
        try:
            desk.load(body.slot)
        except (pedal.PedalError, desk.DeskError) as exc:
            raise HTTPException(409, str(exc)) from exc
        problem = _desk_live_on(desk.record()) if body.live else ""
        return {**_desk_view(), "live_error": problem}

    @app.get("/api/desk")
    def desk_state() -> dict[str, Any]:
        if desk.current() is None:
            names = pedal.known_slots()
            return {"version": None, "live": None,
                    "slots": None if names is None else _slot_list(names)}
        return _desk_view()

    @app.post("/api/desk/live")
    def desk_live(body: DeskSaveIn) -> dict[str, Any]:
        """Turn live edit back on for the open preset (after it timed out or failed)."""
        record = _record(desk.DESK_ID)
        problem = _desk_live_on(record)
        if problem:
            raise HTTPException(409, problem)
        result = live.update(desk.DESK_ID, _live_preset(record, body.edits))
        return {**_desk_view(), **result}

    @app.post("/api/desk/save")
    def desk_save(body: DeskSaveIn) -> dict[str, Any]:
        """Save the pane's edits to the pedal slot they came from."""
        record = _record(desk.DESK_ID)
        chain, changes, preset, _ = _edited(record, body.edits)
        slot = record["pushed_slot"]
        session = live.current()
        if session is not None and session.version_id == desk.DESK_ID:
            try:
                result = live.save(desk.DESK_ID, preset)
            except Exception as exc:
                raise HTTPException(503, f"The pedal didn't save: {exc}") from exc
            if not result["structural"]:
                desk.rebase(chain, preset)
                return {**_desk_view(), "changes": changes, "how": "live"}
        # Block changes, or not live: write the whole preset, as Send to pedal does.

        def work(report: Any) -> dict[str, Any]:
            with contextlib.suppress(Exception):
                _desk_live_off()
            report(f"Writing to slot {pedal.slot_label(slot)}...")
            summary = pedal.push(preset, slot, device_settings=chain.get("device_settings"))
            desk.rebase(chain, preset)
            report("Opening it for live edit again...")
            problem = _desk_live_on(desk.record())
            return {**_desk_view(), "changes": changes, "how": "push", "summary": summary,
                    "live_error": problem}

        return {"job_id": jobs.start(work)}

    @app.post("/api/desk/song")
    def desk_song(body: DeskSongIn) -> dict[str, Any]:
        """Keep the open preset (with the pane's edits) as a new song in the library."""
        record = _record(desk.DESK_ID)
        chain = _edited(record, body.edits)[0] if body.edits else record["chain"]
        name = chain.get("meta", {}).get("name") or "Pedal preset"
        lines = [f"Saved from pedal slot {pedal.slot_label(record['pushed_slot'])} in the pedal "
                 "pane."] + [f"- {n}" for n in record["notes"].splitlines() if n]
        return _new_song_version(
            title=body.title.strip() or name, artist=body.artist.strip(), chain=chain,
            kind="import", label="From the pedal", guitar_id=body.guitar_id,
            rig_id=body.rig_id, origin="import", notes="\n".join(lines))

    @app.post("/api/desk/chat")
    def desk_chat(body: DeskChatIn) -> dict[str, Any]:
        """Ask Claude about the open preset; it may suggest changes to tap."""
        record = _record(desk.DESK_ID)
        if not any(m.role == "user" and m.text.strip() for m in body.messages):
            raise HTTPException(400, "Ask something first.")
        chain, preset = record["chain"], record["preset"]
        if body.edits:
            chain, _, preset, _ = _edited(record, body.edits)
        shown = view.preset_view(chain, preset)
        guitar = lib.guitar(body.guitar_id) if body.guitar_id else None
        prefs = [p["text"] for p in lib.preferences(body.guitar_id, active_only=True)]
        gear = [f"{n['name']} ({n['kind']}): {n['text']}"
                for n in lib.gear_notes(active_only=True) if str(n.get("text") or "").strip()]
        context = {
            "name": chain.get("meta", {}).get("name"),
            "slot": pedal.slot_label(record["pushed_slot"]),
            "guitar": f"{guitar['name']} ({guitar['pickups']})" if guitar else None,
            "preferences": prefs, "gear_notes": gear,
        }
        messages = [m.model_dump() for m in body.messages]
        snapshot = body.snapshot if 0 <= body.snapshot < len(shown["snapshots"]) else 0

        def work(report: Any) -> dict[str, Any]:
            report("Asking Claude...")
            return assistant.ask(shown, snapshot, messages, context, make_caller())

        return {"job_id": jobs.start(work)}

    # -- UI ------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> HTMLResponse:
        # Stamp the script and stylesheet URLs with the running version and
        # forbid caching the page, so a browser never pairs an old page with
        # new code after an update.
        stamp = VERSION["commit"]
        html = (STATIC / "index.html").read_text()
        for asset in ("/static/relay.js", "/static/app.js", "/static/closeup.js",
                      "/static/desk.js", "/static/style.css"):
            html = html.replace(f'"{asset}"', f'"{asset}?v={stamp}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    return app


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
