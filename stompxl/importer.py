"""Songs without Claude: a blank preset, or one read from an .hlx file.

Both produce the same chain spec the generator writes, so everything the app
does with a version -- hand edits, Explain, duplicate, sending to the pedal,
later AI tweaks -- works on them unchanged.
"""

from __future__ import annotations

from typing import Any

from . import engine, xl
from .view import snapshot_editable

#: A neutral starting point for a full rig: a clean Fender-style amp and its cab.
BLANK_AMP = "US Deluxe Nrm"
BLANK_CAB = "1x12 US Deluxe"
DEFAULT_SNAPSHOTS = ("INTRO", "VERSE", "CHORUS", "SOLO")
STARTS = ("amp", "empty")

#: .hlx controller sources for the expression pedals.
_EXP_SOURCES = {1: 1, 2: 2}


class ImportError_(ValueError):
    """The file isn't a preset this app can read."""


def stub_brief(title: str, artist: str, tempo: float, snapshot_names: list[str],
               preset_name: str, tuning: str = "E standard", origin: str = "blank") -> dict[str, Any]:
    """A song plan with nothing in it yet, shaped like the one Claude writes."""
    empty = {"recommended": False, "how": ""}
    return {
        "title": title, "artist": artist, "preset_name": preset_name, "tuning": tuning,
        "tempo_bpm": tempo, "reference_gear": "", "tone_summary": "", "guitar_adaptation": "",
        "sections": [{"name": n, "role": "", "description": "", "pickup": "", "guitar_knobs": ""}
                     for n in snapshot_names],
        "wah": empty, "expression": empty, "looper": empty, "capo": empty,
        "playing_tips": [],
        # Marks a plan nobody has written: the UI offers to have Claude fill it in.
        "origin": origin,
    }


def _gate(gate_mode: str) -> dict[str, Any]:
    on = gate_mode != "off"
    return {"enabled": on, "threshold_db": -55.0 if on else -48.0,
            "why": "Your default gate setting." if on else "Gate off by default."}


def blank_chain(name: str, rig_kind: str, start: str, gate_mode: str,
                tempo: float) -> dict[str, Any]:
    """An empty preset, or a clean amp and cab for a full rig."""
    blocks: list[dict[str, Any]] = []
    if start == "amp" and rig_kind == "full":
        blocks = [{"model": BLANK_AMP, "why": "Clean starting amp.", "parameters": {}},
                  {"model": BLANK_CAB, "why": "The amp's own cab.", "parameters": {}}]
    gate = _gate(gate_mode)
    return {
        "title": name, "meta": {"name": name}, "global": {"@tempo": float(tempo)},
        "input": {"noiseGate": gate["enabled"], "threshold": gate["threshold_db"],
                  "decay": xl.GATE_DECAY_DEFAULT},
        "gate": gate,
        "blocks": blocks,
        "snapshots": [{"name": n, "blocks": {}} for n in DEFAULT_SNAPSHOTS],
    }


# ---------------------------------------------------------------------------
# .hlx


def hlx_to_chain(hlx: dict[str, Any], *, captures: list[dict[str, Any]] | None = None,
                 ) -> tuple[dict[str, Any], list[str]]:
    """The chain an .hlx describes, and notes on anything that didn't carry over.

    ``captures`` (the player's NAM captures, xl.parse_captures) name the NAM A2
    Lite blocks' captures, which the firmware stores as Xenomorph Fuzz (see
    _mark_nam).
    """
    cat = engine.catalog()
    try:
        data = hlx["data"]
        tone = data["tone"]
        dsp0 = tone["dsp0"]
    except (KeyError, TypeError) as exc:
        raise ImportError_("That isn't a Helix preset (.hlx) file") from exc
    notes: list[str] = []
    device = data.get("device")
    if device not in (None, xl.DEVICE_ID):
        notes.append(f"Made for another Helix device ({device}); imported what the Stomp XL can run.")
    if any(isinstance(tone.get(k), dict) and _blocks_of(tone[k]) for k in ("dsp1",)):
        notes.append("Its second DSP's blocks were left out: the Stomp XL has one DSP.")

    raw_blocks = _blocks_of(dsp0)
    if any(b.get("@path", 0) for _, b in raw_blocks):
        notes.append("It used parallel paths; they're flattened into one chain, path A first.")
    raw_blocks.sort(key=lambda kb: (kb[1].get("@path", 0), kb[1].get("@position", 0)))

    blocks: list[dict[str, Any]] = []
    index_of: dict[str, int] = {}  # .hlx block key -> chain index
    dropped: dict[str, list[str]] = {}
    for key, raw in raw_blocks:
        entries = [(key, raw)]
        cab_key = raw.get("@cab")
        if isinstance(cab_key, str) and isinstance(dsp0.get(cab_key), dict):
            entries.append((cab_key, dsp0[cab_key]))  # an amp's own cab, after it
        for entry_key, entry in entries:
            if len(blocks) >= xl.MAX_BLOCKS:
                notes.append(f"Only the first {xl.MAX_BLOCKS} blocks fit; "
                             f"{entry.get('@model')} was left out.")
                continue
            symbol = str(entry.get("@model", ""))
            # HX Edit writes the variant (…Mono / …Stereo); the catalog doesn't.
            model = xl.model_for_symbol(symbol, cat) if symbol else None
            if model is None:
                notes.append(f"{symbol or 'A block'} isn't a model this app knows, so it was left out.")
                continue
            params = {}
            for name, value in entry.items():
                if name.startswith("@"):
                    continue
                if name in model.parameters:
                    # Every value, defaults included: an amp's paired cab has
                    # its own defaults, unlike the standalone cab the catalog
                    # describes, so a "default" left out comes back different.
                    params[name] = value
                else:
                    dropped.setdefault(model.display_name, []).append(name)
            block: dict[str, Any] = {"model": model.display_name, "why": "Imported.",
                                     "parameters": params}
            if entry.get("@enabled") is False:
                block["enabled"] = False
            if entry.get("@no_snapshot_bypass"):
                block["no_snapshot_bypass"] = True
            if entry.get("@stereo") is True or symbol.endswith("Stereo"):
                # Keep the variant: without it the block is rebuilt as Mono, a
                # different model on the pedal (and a mono delay or reverb).
                block["stereo"] = True
            index_of[entry_key] = len(blocks)
            blocks.append(block)
    for model_name, names in dropped.items():
        notes.append(f"{model_name}: {', '.join(sorted(set(names)))} not imported "
                     "(the app has no data for them; the pedal keeps its own default).")

    nam = _mark_nam(blocks, captures or [])
    _mark_stage_boost(blocks, cat)
    _import_footswitches(tone, blocks, index_of, cat)
    _import_expression(tone, blocks, index_of, cat)
    snapshots = _import_snapshots(tone, blocks, index_of, notes)

    name = str((data.get("meta") or {}).get("name") or "Imported")[: xl.PRESET_NAME_LIMIT]
    tempo = float((tone.get("global") or {}).get("@tempo") or 120.0)
    chain: dict[str, Any] = {
        "title": name, "meta": {"name": name}, "global": {"@tempo": tempo},
        "blocks": blocks, "snapshots": snapshots,
    }
    if nam:
        chain["nam"] = nam
    gate_in = dsp0.get("inputA")
    if isinstance(gate_in, dict):
        section = {k: gate_in[k] for k in ("noiseGate", "threshold", "decay") if k in gate_in}
        chain["input"] = {**xl.IO_DEFAULTS["input"], **section}
        chain["gate"] = {"enabled": bool(chain["input"]["noiseGate"]),
                         "threshold_db": float(chain["input"]["threshold"]), "why": "Imported."}
    settings = data.get("device_settings")
    if isinstance(settings, dict) and settings:
        chain["device_settings"] = settings  # read off the pedal; written back on send
    join = dsp0.get("join")
    if isinstance(join, dict):
        mix = {k: v for k, v in join.items() if k in xl.JOIN_PARAMS}
        if mix:
            chain["join"] = mix
    out = dsp0.get("outputA")
    if isinstance(out, dict):
        chain["output"] = {**xl.IO_DEFAULTS["output"],
                           **{k: out[k] for k in ("gain", "pan") if k in out}}
    return chain, notes


def _mark_stage_boost(blocks: list[dict[str, Any]], cat: Any) -> None:
    """The app's Stage boost, read back: its drive just before the amp that the
    snapshots leave alone."""
    at = engine.stage_boost_at(blocks, [cat.get(b["model"]) for b in blocks])
    if at:
        before = blocks[at - 1]
        if before["model"] == xl.STAGE_BOOST_MODEL and before.get("no_snapshot_bypass"):
            before["stage_boost"] = True


def _mark_nam(blocks: list[dict[str, Any]], captures: list[dict[str, Any]]
              ) -> dict[str, Any] | None:
    """Mark the NAM A2 Lite blocks in an imported chain, in place.

    The NAM firmware writes its block as a Xenomorph Fuzz whose Min Freq knob is
    the capture's IR slot, so the preset alone can't tell NAM from a real fuzz.
    It's almost always NAM here, so every Xenomorph Fuzz whose Min Freq is a
    whole IR slot number counts as NAM; the chain's "Is a fuzz" switch (the
    ``{block, nam}`` edit) turns one back into a fuzz. Returns the chain's
    ``nam`` entry (mode and capture, as the generator writes it).
    """
    for block in blocks:
        if block["model"] == xl.NAM_MODEL and xl.nam_slot(block) is not None:
            block["nam"] = True
            capture = xl.nam_capture(xl.nam_slot(block), captures)
            block["why"] = f"Imported {xl.NAM_TITLE} block: the '{capture['name']}' capture is the amp."
    return xl.nam_entry(blocks, captures)


def _blocks_of(dsp: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(k, v) for k, v in dsp.items()
            if k.startswith("block") and isinstance(v, dict) and v.get("@model")]


def _import_footswitches(tone: dict[str, Any], blocks: list[dict[str, Any]],
                         index_of: dict[str, int], cat: Any) -> None:
    by_fs = {v: k for k, v in xl.switch_map().items()}
    for key, fs in ((tone.get("footswitch") or {}).get("dsp0") or {}).items():
        if key not in index_of or not isinstance(fs, dict):
            continue
        number = fs.get("@fs_index")
        switch = by_fs.get(number)
        if switch is None:
            continue
        block = blocks[index_of[key]]
        block.update(footswitch=number, switch=switch,
                     footswitch_label=str(fs.get("@fs_label") or "")[:16]
                     or xl.switch_label(cat.get(block["model"])))


def _import_expression(tone: dict[str, Any], blocks: list[dict[str, Any]],
                       index_of: dict[str, int], cat: Any) -> None:
    for key, params in ((tone.get("controller") or {}).get("dsp0") or {}).items():
        if key not in index_of or not isinstance(params, dict):
            continue
        block = blocks[index_of[key]]
        model = cat.get(block["model"])
        for name, entry in params.items():
            pedal = _EXP_SOURCES.get(entry.get("@controller")) if isinstance(entry, dict) else None
            if pedal is None or name not in model.parameters or block.get("expression"):
                continue
            block["expression"] = pedal
            if name != xl.PEDAL_PARAMETER:
                block["expression_param"] = name
            span = model.get_parameter(name).controller_range()
            low, high = entry.get("@min"), entry.get("@max")
            if span and (low, high) != tuple(span) and low is not None and high is not None:
                block["expression_range"] = [low, high]


def _import_snapshots(tone: dict[str, Any], blocks: list[dict[str, Any]],
                      index_of: dict[str, int], notes: list[str]) -> list[dict[str, Any]]:
    global_tempo = float((tone.get("global") or {}).get("@tempo") or 120.0)
    found = sorted((int(k[8:]), v) for k, v in tone.items()
                   if k.startswith("snapshot") and k[8:].isdigit() and isinstance(v, dict))
    valid = [s for _, s in found if s.get("@valid", True)]
    if len(valid) > xl.SNAPSHOT_COUNT:
        notes.append(f"It had {len(valid)} snapshots; the Stomp XL keeps the first {xl.SNAPSHOT_COUNT}.")
    out = []
    for number, snap in enumerate(valid[: xl.SNAPSHOT_COUNT]):
        changes: dict[str, dict[str, Any]] = {}
        states = (snap.get("blocks") or {}).get("dsp0") or {}
        for key, on in states.items():
            index = index_of.get(key)
            if index is None or blocks[index].get("no_snapshot_bypass"):
                continue
            if bool(on) != (blocks[index].get("enabled", True) is not False):
                changes.setdefault(str(index), {})["enabled"] = bool(on)
        values = (snap.get("controllers") or {}).get("dsp0") or {}
        for key, params in values.items():
            index = index_of.get(key)
            if index is None or not isinstance(params, dict):
                continue
            model = engine.catalog().get(blocks[index]["model"])
            for name, entry in params.items():
                value = entry.get("@value") if isinstance(entry, dict) else None
                # Kept even when it matches the block's own value, so a setting
                # under snapshot control in the original stays that way.
                if (value is None or name not in model.parameters
                        or not snapshot_editable(name, model.get_parameter(name))):
                    continue
                changes.setdefault(str(index), {}).setdefault("parameters", {})[name] = value
        name = str(snap.get("@name") or f"SNAPSHOT {number + 1}")[: xl.SNAPSHOT_NAME_LIMIT]
        entry: dict[str, Any] = {"name": name, "blocks": changes}
        tempo = snap.get("@tempo")
        if isinstance(tempo, (int, float)) and abs(float(tempo) - global_tempo) > 1e-6:
            entry["tempo"] = float(tempo)  # this snapshot plays at its own tempo
        out.append(entry)
    while len(out) < xl.SNAPSHOT_COUNT:
        out.append({"name": f"SNAPSHOT {len(out) + 1}", "blocks": {}})
    return out
