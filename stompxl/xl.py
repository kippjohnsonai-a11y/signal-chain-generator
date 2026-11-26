"""What is specific to the HX Stomp XL, as opposed to HelixGen's HX Stomp.

The two share a model catalog, block budget and file format. The XL differs in
its device id, its four snapshots (the Stomp has three) and its switch count.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

from stompxl.helixgen import resources

DEVICE_ID = 2162699
PRODUCT_ID = 0x4253
SNAPSHOT_COUNT = 4
SNAPSHOT_NAME_LIMIT = 10
MAX_BLOCKS = 8
PRESET_NAME_LIMIT = 16
#: Preset slots: 32 banks of four, 01A-32D on the panel, 0-127 on the wire
#: (HX Stomp XL owner's manual, "Preset List").
SLOT_COUNT = 128

#: Controller sources as stored in ``tone.controller``, read off presets HX Edit
#: saved on a Stomp XL: Snapshots is 12 (the HX Stomp uses 9), EXP 1 is 1.
#: EXP 2 = 2 follows the same numbering.
CONTROLLER_SNAPSHOT = 12
CONTROLLER_EXP1 = 1
CONTROLLER_EXP2 = 2

#: Physical footswitches: top row FS1 FS2 FS3 MODE, bottom row FS4 FS5 FS6 TAP.
#: The recommended setup (Global Settings > Footswitches > Stomp Mode "4 Switch",
#: Up/Down Switches "Snapshots") turns FS1/FS4 into snapshot up/down, which
#: steps through the song's sections in order. That leaves FS2, FS3, FS5 and
#: FS6 as stomps: the looper on FS6, the rest bottom row first.
FOOTSWITCH_COUNT = 6
LOOPER_SWITCH = 6
SNAPSHOT_UPDOWN_SWITCHES = (1, 4)
SWITCH_FILL_ORDER = (5, 2, 3, 6)
#: Physical switch -> the ``@fs_index`` written to the preset (one-based
#: position in the device's footswitch array). Confirmed on a Stomp XL: a
#: preset HX Edit saved with blocks on FS4, FS5 and FS6 holds them at
#: positions 4, 5 and 6. Override with the ``footswitch_map`` setting.
DEFAULT_SWITCH_MAP = {n: n for n in range(1, FOOTSWITCH_COUNT + 1)}

#: Which blocks earn a stomp switch, most-used first. Amps, cabs, routing and
#: the expression-driven volume pedal never get one.
SWITCH_PRIORITY = ("Wah", "Distortion", "Delay", "boost", "Modulation", "Pitch/Synth",
                   "Reverb", "Filter", "Dynamics", "EQ")
BOOST_MODEL = "Gain"
BOOST_DB = 4.0
#: The Stage boost: a drive before the amp, off until Stage mode's "Gain boost"
#: switches it on. It has no footswitch and the snapshots leave it alone.
STAGE_BOOST_MODEL = "Kinky Boost"

#: The player's expression pedal setup (a setting).
#: "single": one ordinary TRS pedal (e.g. Moog EP-3) on EXP 1. Per the owner's
#:   manual, everything it sweeps goes on EXP 1, so a preset holds one swept block.
#: "mission": Line 6 Mission SP1-L6H; its toe switch flips EXP 1 (wah/whammy)
#:   and EXP 2 (volume).
#: "dual": two ordinary pedals on a Y cable, EXP 1 and EXP 2.
EXP_PEDAL_SETUPS = {
    "single": "One expression pedal (e.g. Moog EP-3)",
    "mission": "Line 6 Mission SP1-L6H (toe switch)",
    "dual": "Two expression pedals (Y cable)",
}
DEFAULT_EXP_PEDAL = "single"

#: The Input block's built-in noise gate (free: it isn't one of the 8 blocks).
#: A setting: "on" (default) always gates, tuned per song and guitar; "auto"
#: lets Claude decide; "off" never gates.
GATE_MODES = {
    "on": "On for every preset (tuned per song and guitar)",
    "auto": "Claude decides per song",
    "off": "Off",
}
DEFAULT_GATE = "on"
GATE_THRESHOLD_RANGE = (-96.0, 0.0)
GATE_DECAY_DEFAULT = 0.5

#: The Input and main Output blocks as the editor shows them:
#: (key in the preset, label, kind, (min, max, step) in display units, scale).
#: Stored values are display / scale; Decay and Pan are 0-1 shown as 0-100.
IO_PARAMS: dict[str, list[tuple[str, str, str, tuple[float, float, float] | None, float]]] = {
    "input": [
        ("noiseGate", "Gate", "toggle", None, 1),
        ("threshold", "Threshold (dB)", "number", (-96, 0, 1), 1),
        ("decay", "Decay", "number", (0, 100, 1), 100),
    ],
    "output": [
        ("gain", "Level (dB)", "number", (-60, 6, 0.5), 1),
        ("pan", "Pan (50 = centre)", "number", (0, 100, 1), 100),
    ],
}
IO_MODELS = {"input": "HX Stomp Input", "output": "HX Stomp Output Main"}
IO_TITLES = {"input": "Input", "output": "Output"}
IO_DEFAULTS = {
    "input": {"noiseGate": False, "threshold": -48.0, "decay": GATE_DECAY_DEFAULT},
    "output": {"gain": 0.0, "pan": 0.5},
}

#: Rough DSP cost of each block, in percent of the Stomp XL's single DSP.
#: The budget starts at DSP_BUDGET and is lowered by every refusal the pedal
#: reports (see learn_dsp_refusal), so it converges on what the pedal holds.
#: These are conservative estimates, not Line 6 figures: they exist to catch
#: chains the pedal will refuse (a poly-pitch block plus an amp, a premium
#: reverb and a looper overflowed on a real push) before Claude's answer is
#: accepted. Tune DSP_BUDGET if the pedal turns out to fit more or less.
DSP_BUDGET = 100.0
_DSP_BY_CATEGORY = {
    "Amp": 30.0, "Cab": 8.0, "Distortion": 4.0, "Dynamics": 3.0, "Modulation": 6.0,
    "Delay": 8.0, "Reverb": 10.0, "Pitch/Synth": 12.0, "Filter": 6.0, "Wah": 4.0,
    "EQ": 2.0, "Volume/Pan": 1.0, "Routing": 2.0,
}
_PREMIUM_REVERBS = {"Glitz", "Ganymede", "Searchlights", "Plateaux", "Hot Springs",
                    "Double Tank", "Shimmer", "Particle Reverb", "Heliosphere", "Dynamic Hall",
                    "Dynamic Plate", "Dynamic Room", "Dynamic Ambience", "Dynamic Bloom"}
_HEAVY_DELAYS = {"Poly Sustain": 15.0, "Glitch Delay": 12.0, "Euclidean Delay": 12.0}


def dsp_cost(model: Any) -> float:
    name, category = model.display_name, (model.category or "")
    if name == NAM_MODEL:
        return NAM_DSP
    if name.startswith("Poly ") and name not in _HEAVY_DELAYS:
        return 40.0      # polyphonic pitch: the heaviest family on the platform
    if name == LOOPER_MODEL:
        return 8.0
    if "Preamp" in name:
        return 15.0
    if name in _HEAVY_DELAYS:
        return _HEAVY_DELAYS[name]
    if category == "Reverb" and name in _PREMIUM_REVERBS:
        return 18.0
    if category == "Cab" and "Dual" in name:
        return 16.0
    return _DSP_BY_CATEGORY.get(category, 5.0)


def dsp_total(models: list[Any]) -> float:
    return sum(dsp_cost(m) for m in models)


#: Never learn a budget below this: a refusal can also come from one odd block.
DSP_BUDGET_FLOOR = 60.0


def dsp_budget(settings: dict[str, Any] | None = None) -> float:
    """The working budget: the default, or lower if the pedal has refused more."""
    learned = (settings or {}).get("dsp_budget")
    try:
        return min(DSP_BUDGET, float(learned)) if learned is not None else DSP_BUDGET
    except (TypeError, ValueError):
        return DSP_BUDGET


#: Typical blocks of each kind, for the "what still fits" panel: (label, examples, cost).
ROOM_KINDS = [
    ("Drive / fuzz", "Minotaur, Scream 8, Arbitrator Fuzz", _DSP_BY_CATEGORY["Distortion"]),
    ("Compressor", "Deluxe Comp, Red Squeeze", _DSP_BY_CATEGORY["Dynamics"]),
    ("EQ", "Simple EQ, Parametric", _DSP_BY_CATEGORY["EQ"]),
    ("Modulation", "chorus, phaser, flanger, tremolo", _DSP_BY_CATEGORY["Modulation"]),
    ("Filter", "auto-wah, envelope filter", _DSP_BY_CATEGORY["Filter"]),
    ("Delay", "Simple Delay, Transistor Tape, Vintage Digital", _DSP_BY_CATEGORY["Delay"]),
    ("Reverb", "Plate, Room, '63 Spring, Hall", _DSP_BY_CATEGORY["Reverb"]),
    ("Other pitch / synth", "Pitch Wham, Simple Pitch, Twin Harmony", _DSP_BY_CATEGORY["Pitch/Synth"]),
    ("Premium reverb", "Glitz, Ganymede, Searchlights, Plateaux", 18.0),
    ("Second Poly block", "Poly Pitch, Poly Detune, Poly Wham", 40.0),
]
#: The order a sample chain is filled from what's left, most useful first.
_ROOM_EXAMPLE = ("Drive / fuzz", "Delay", "Reverb", "Compressor", "Modulation", "EQ")


def dsp_room(rig_kind: str, options: dict[str, Any], settings: dict[str, Any] | None = None
             ) -> dict[str, Any]:
    """What the rig and toggles already spend, and what else still fits.

    Mirrors the rules check_blocks enforces: the amp and cab of a full rig,
    the 4-cable method's FX Loop, the NAM block (and a cab, unless the
    full-rig option keeps to captures that include theirs) of a NAM-on-pedal
    rig, the Return block of a NAM-on-computer rig, and one block per toggle
    that is on.
    """
    reserved: list[tuple[str, float]] = []
    if options.get("capo"):
        reserved.append((CAPO_MODEL, 40.0))
    if options.get("wah"):
        reserved.append(("Wah", _DSP_BY_CATEGORY["Wah"]))
    elif options.get("expression"):
        reserved.append(("Volume pedal (expression)", _DSP_BY_CATEGORY["Volume/Pan"]))
    if rig_kind == "full":
        reserved += [("Amp", _DSP_BY_CATEGORY["Amp"]), ("Cab", _DSP_BY_CATEGORY["Cab"])]
    elif rig_kind == "4cm":
        reserved.append((FX_LOOP_MODEL, _DSP_BY_CATEGORY["Routing"]))
    elif rig_kind == "nam_pedal":
        reserved.append((NAM_TITLE, NAM_DSP))
        if not options.get("full_rig"):  # a full-rig capture brings its own cab
            reserved.append(("Cab", _DSP_BY_CATEGORY["Cab"]))
    elif rig_kind == "nam_usb":
        reserved.append((RETURN_MODEL + ": guitar in", _DSP_BY_CATEGORY["Routing"]))
    if options.get("looper"):
        reserved.append(("Looper", 8.0))

    budget = dsp_budget(settings)
    used = sum(cost for _, cost in reserved)
    left = max(0.0, budget - used)
    slots = max(0, MAX_BLOCKS - len(reserved))
    kinds = [
        {"kind": label, "examples": examples, "cost": cost,
         "max": min(slots, int(left // cost)) if cost else slots}
        for label, examples, cost in ROOM_KINDS
    ]
    example: list[str] = []
    spare, free = left, slots
    for label in _ROOM_EXAMPLE:
        cost = next(c for name, _, c in ROOM_KINDS if name == label)
        if free and cost <= spare:
            example.append(label)
            spare -= cost
            free -= 1
    return {
        "budget": budget, "used": used, "left": left, "slots_left": slots,
        "reserved": [{"name": n, "cost": c} for n, c in reserved],
        "kinds": kinds, "example": example, "example_cost": left - spare,
    }


def describe_room(room: dict[str, Any]) -> str:
    """dsp_room as one prompt line for Claude."""
    fits = [f"{k['kind']} ~{k['cost']:.0f}%" for k in room["kinds"] if k["max"]]
    out = [k["kind"] for k in room["kinds"] if not k["max"]]
    return (
        f"DSP room: the required blocks ({', '.join(r['name'] for r in room['reserved']) or 'none'}) "
        f"use about {room['used']:.0f}% of {room['budget']:.0f}%, leaving about {room['left']:.0f}% "
        f"and {room['slots_left']} block slots. Still fits: {', '.join(fits) or 'nothing more'}."
        + (f" Won't fit: {', '.join(out)}." if out else "")
    )


def model_for_symbol(symbol: str, catalog: Any) -> Any | None:
    """The catalog model behind a device symbol (which adds a Mono/Stereo suffix)."""
    for suffix in ("Mono", "Stereo", ""):
        if suffix and not symbol.endswith(suffix):
            continue
        host = symbol[: len(symbol) - len(suffix)] if suffix else symbol
        for model in catalog.models():
            if model.internal_name == host:
                return model
    return None


def learn_dsp_refusal(placed: list[str], refused: str, catalog: Any,
                      settings: dict[str, Any] | None = None) -> float | None:
    """The new budget after the pedal refused ``refused`` with ``placed`` already in.

    What was attempted didn't fit, so the budget becomes just under its
    estimate (never above the current budget, never below the floor).
    Returns the new budget, or None if the symbols couldn't be costed.
    """
    models = [model_for_symbol(s, catalog) for s in [*placed, refused]]
    if any(m is None for m in models):
        return None
    attempted = dsp_total(models)
    return max(DSP_BUDGET_FLOOR, min(dsp_budget(settings), attempted - 1.0))


def is_stereo_only(parameter: str) -> bool:
    """Pan/spread controls only the stereo variant of a block has. Blocks go to the
    pedal in mono, where these can't be snapshot-controlled."""
    return parameter.startswith("Pan") or parameter in ("Spread", "Width")


WAH_CATEGORY = "Wah"
LOOPER_MODEL = "One Switch Looper"
CAPO_MODEL = "Poly Capo"
VOLUME_MODEL = "Volume"
FX_LOOP_MODEL = "FX Loop (Mono)"
#: Blocks whose sweep parameter belongs on an expression pedal.
PEDAL_PARAMETER = "Pedal"

# ---------------------------------------------------------------------------
# Neural Amp Modeler (NAM)
#
# Two rigs hand the amp to NAM:
#
# - "nam_pedal": NAM runs on the pedal, through the community NAM A2 Lite
#   firmware (github.com/nocaseguy/hx-stomp-nam-lite, a patch of stock 3.80.0).
#   That firmware puts NAM in Xenomorph Fuzz Mono's place, so presets, HX Edit
#   and this app all carry it as a "Xenomorph Fuzz" block whose knobs mean
#   NAM's (NAM_KNOBS). Captures live in User IR slots; the Model knob picks one.
# - "nam_usb": NAM runs on the computer. One preset sandwiches it: the guitar
#   comes in on the Return L jack (a Return block at the head of path B), path
#   B's pre-amp effects go out of the Send output to the computer over USB, NAM
#   comes back on USB 5/6 as the Input block, and path A adds the post-amp
#   effects. See docs/nam.md.

RIG_KINDS = ("full", "front", "4cm", "nam_pedal", "nam_usb")
NAM_RIGS = ("nam_pedal", "nam_usb")
NAM_MODEL = "Xenomorph Fuzz"
NAM_TITLE = "NAM A2 Lite"
#: One NAM A2 Lite block with its tone stack, measured on an HX Stomp.
NAM_DSP = 23.0
NAM_MAX_BLOCKS = 3
#: The NAM firmware's knobs, by the Xenomorph Fuzz parameter that carries them.
NAM_KNOBS = {
    "Gain": "Input", "Tone": "Treble", "Level": "Output", "Clipping": "Polarity",
    "OscLevel": "Bass", "OscTone": "Mid", "MinFreq": "Model (IR slot)",
    "MaxFreq": "Mid Freq", "Sensitivity": "High Cut",
}
#: A flat tone stack at the capture's own level: Input, Treble, Output, Bass and
#: Mid at 5.0, Polarity normal, Mid Freq 650 Hz, High Cut off.
NAM_DEFAULTS = {"Gain": 0.5, "Tone": 0.5, "Level": 0.5, "Clipping": 0, "OscLevel": 0.5,
                "OscTone": 0.5, "MaxFreq": 650.0, "Sensitivity": 1.0}
NAM_SLOT_PARAMETER = "MinFreq"
IR_SLOTS = 128
RETURN_MODEL = "Return L (Mono)"
#: Blocks that belong after the amp: from the first of these on, a chain is
#: "post-amp" (path A of the USB sandwich; after the NAM block on the pedal).
POST_AMP_CATEGORIES = ("Cab", "Modulation", "Delay", "Reverb", "Volume/Pan")
#: Blocks that must come before the amp.
PRE_AMP_CATEGORIES = ("Distortion", "Wah", "Dynamics")


def is_post_amp(model: Any) -> bool:
    return ((model.category or "") in POST_AMP_CATEGORIES
            or model.display_name in (LOOPER_MODEL, BOOST_MODEL))


def amp_slot(models: list[Any]) -> int:
    """Where the amp goes in a chain without one: before the first post-amp block."""
    return next((i for i, m in enumerate(models) if is_post_amp(m)), len(models))


def nam_split(blocks: list[dict[str, Any]], models: list[Any]) -> int:
    """Where NAM goes in a chain: before the first block on its far side.

    A block Claude marked ``amp_side`` 'after' is past the amp; an unmarked one
    (added by hand, or from before the marks) goes by its kind (is_post_amp).
    """
    for i, (block, model) in enumerate(zip(blocks, models, strict=True)):
        side = block.get("amp_side")
        if side == "after" or (side != "before" and is_post_amp(model)):
            return i
    return len(models)


def nam_slot(block: dict[str, Any]) -> int | None:
    """The IR slot on a NAM block's Model knob, or None when Min Freq isn't one
    (a real Xenomorph Fuzz's frequency). The pedal stores it as a float32."""
    value = (block.get("parameters") or {}).get(NAM_SLOT_PARAMETER)
    if not isinstance(value, (int, float)) or abs(value - round(value)) > 0.01:
        return None
    slot = round(value)
    return slot if 1 <= slot <= IR_SLOTS else None


def nam_capture(slot: int | None, captures: list[dict[str, Any]]) -> dict[str, Any]:
    """The player's capture in ``slot``, or a stand-in naming the slot."""
    found = next((c for c in captures if slot is not None and c.get("slot") == slot), None)
    return found or {"name": f"IR slot {slot}", "slot": slot, "cab": False}


def nam_entry(blocks: list[dict[str, Any]], captures: list[dict[str, Any]]
              ) -> dict[str, Any] | None:
    """A chain's ``nam`` entry for NAM on the pedal, from its first NAM block."""
    first = next((b for b in blocks if b.get("nam")), None)
    if first is None:
        return None
    return {"mode": "pedal", "capture": nam_capture(nam_slot(first), captures)}


#: Kept on a capture the app downloaded: its TONE3000 tone and model, and its files.
CAPTURE_SOURCE_KEYS = ("tone_id", "model", "nam_file", "ir_file")


def parse_captures(raw: Any) -> list[dict[str, Any]]:
    """The player's NAM captures from settings, cleaned.

    Each is ``{"name", "slot", "description", "cab"}``: ``slot`` is the User IR
    slot (1-128) holding the converted capture for the pedal, or None for one
    only on the computer; ``cab`` says the capture already includes a cabinet.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:40]
        if not name or name.lower() in seen:
            continue
        slot = entry.get("slot")
        try:
            slot = int(slot) if slot not in (None, "") else None
        except (TypeError, ValueError):
            slot = None
        if slot is not None and not 1 <= slot <= IR_SLOTS:
            slot = None
        seen.add(name.lower())
        capture = {"name": name, "slot": slot,
                   "description": str(entry.get("description") or "").strip()[:200],
                   "cab": bool(entry.get("cab"))}
        for key in CAPTURE_SOURCE_KEYS:  # where the app downloaded it from, if it did
            if entry.get(key) not in (None, ""):
                capture[key] = entry[key]
        out.append(capture)
    return out


def captures_for(rig_kind: str, settings: dict[str, Any] | None, *, full_rig: bool = False
                 ) -> list[dict[str, Any]]:
    """The captures a rig can play: the pedal needs ones loaded in an IR slot.

    ``full_rig`` keeps only captures that include their cab, so the chain needs
    no cab block (for a song whose Poly Capo leaves no DSP for one).
    """
    captures = parse_captures((settings or {}).get("nam_captures"))
    if full_rig:
        captures = [c for c in captures if c["cab"]]
    if rig_kind == "nam_pedal":
        return [c for c in captures if c["slot"] is not None]
    return captures if rig_kind == "nam_usb" else []


def describe_capture(capture: dict[str, Any]) -> str:
    where = f"IR slot {capture['slot']}" if capture.get("slot") else "computer only"
    return (f"{capture['name']} ({where}; "
            + ("includes its cab" if capture.get("cab") else "amp only, needs a cab")
            + (f"; {capture['description']}" if capture.get("description") else "") + ")")


#: The two USB-sandwich values no public source pins down: the Input block's
#: number for "USB 5/6" and path B's Output block number for "Send L/R". They
#: are learnt once from a preset the player sets up in HX Edit
#: (``stompxl learn-nam-usb``) and kept in the ``nam_usb_routing`` setting.
def usb_routing(settings: dict[str, Any] | None) -> dict[str, int] | None:
    raw = (settings or {}).get("nam_usb_routing")
    if not isinstance(raw, dict):
        return None
    try:
        return {"input": int(raw["input"]), "send_output": int(raw["send_output"])}
    except (KeyError, TypeError, ValueError):
        return None


def learn_usb_routing(hlx: dict[str, Any]) -> dict[str, int]:
    """The USB 5/6 input and Send output numbers from a hand-built sandwich preset.

    The preset must have its Input block on USB 5/6 (anything but the main
    input) and path B going out of its own Send output.
    """
    try:
        dsp0 = hlx["data"]["tone"]["dsp0"]
        source = int(dsp0["inputA"]["@input"])
        send = int(dsp0["outputB"]["@output"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("That isn't a Stomp preset (.hlx) with an Input and two outputs") from exc
    if source in (0, 1):
        raise ValueError("The preset's Input block is still on the main input: set it to USB 5/6")
    if send == 0:
        raise ValueError("Path B isn't routed to its own output: set path B's Output to Send L/R")
    return {"input": source, "send_output": send}


@lru_cache(maxsize=1)
def _base_template() -> dict[str, Any]:
    with open(resources.template_path(), encoding="utf-8") as handle:
        return json.load(handle)


def template() -> dict[str, Any]:
    """HelixGen's HX Stomp template, re-badged as a Stomp XL with four snapshots."""

    preset = copy.deepcopy(_base_template())
    data = preset["data"]
    data["device"] = DEVICE_ID
    tone = data["tone"]
    for index in range(SNAPSHOT_COUNT):
        key = f"snapshot{index}"
        if key not in tone:
            snapshot = copy.deepcopy(tone["snapshot0"])
            snapshot["@name"] = f"SNAPSHOT {index + 1}"
            snapshot["@custom_name"] = False
            tone[key] = snapshot
        tone[key]["@valid"] = True
    # HelixGen's template mixes the paths at +3 dB; HX Edit's own default, and
    # every preset read off the pedal so far, is 0 dB.
    tone["dsp0"]["join"]["Level"] = 0.0
    return preset


#: The path-mix block's settings, in the preset and over USB.
JOIN_PARAMS = ("A Level", "A Pan", "B Level", "B Pan", "B Polarity", "Level")


def assign_expression(
    preset: dict[str, Any], chain: dict[str, Any], catalog: Any
) -> list[str]:
    """Put each wah, volume and whammy-style sweep on an expression pedal.

    The chain's blocks may carry ``"expression": 1`` or ``2``; otherwise a wah or
    pitch-wham sweep goes to EXP 1 and a volume pedal to EXP 2. Returns what was
    assigned, for the version notes.
    """

    tone = preset["data"]["tone"]
    dsp0 = tone.get("dsp0", {})
    assigned: list[str] = []
    for index, block in enumerate(chain.get("blocks", [])):
        spec = block if isinstance(block, dict) else {"model": block}
        exp = spec.get("expression")
        if exp in (None, 0, False):
            continue
        model = catalog.get(spec["model"])
        # A hand edit can put any parameter on the pedal, over part of its range.
        param = spec.get("expression_param") or PEDAL_PARAMETER
        if param not in model.parameters:
            continue
        definition = model.get_parameter(param)
        span = spec.get("expression_range") or definition.controller_range() or (0.0, 1.0)
        block_key = f"block{index}"
        if block_key not in dsp0:
            continue
        source = CONTROLLER_EXP2 if exp == 2 else CONTROLLER_EXP1
        tone.setdefault("controller", {}).setdefault("dsp0", {}).setdefault(block_key, {})[
            param
        ] = {"@controller": source, "@min": span[0], "@max": span[1]}
        assigned.append(f"EXP {2 if exp == 2 else 1} -> {model.display_name} {param}")
    return assigned


def default_expression(model: Any) -> int | None:
    """The pedal a block's sweep normally lives on, or None if it has no sweep."""

    if PEDAL_PARAMETER not in model.parameters:
        return None
    return 2 if model.display_name == VOLUME_MODEL else 1


def switch_map(settings: dict[str, Any] | None = None) -> dict[int, int]:
    """Physical switch -> preset ``@fs_index``, with any calibrated overrides."""
    mapping = dict(DEFAULT_SWITCH_MAP)
    for key, value in ((settings or {}).get("footswitch_map") or {}).items():
        try:
            mapping[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return mapping


def switch_role(model: Any) -> str | None:
    """The priority bucket a block's footswitch falls in, or None for no switch."""
    if model.display_name == BOOST_MODEL:
        return "boost"
    if model.display_name in (VOLUME_MODEL, LOOPER_MODEL, NAM_MODEL, RETURN_MODEL):
        return None
    category = model.category or ""
    return category if category in SWITCH_PRIORITY else None


def plan_footswitches(models: list[Any], skip: Any = ()) -> dict[int, int]:
    """Block index -> physical switch.

    The looper always sits on FS6. Every other switchable block is ranked by
    how often it gets stomped live and placed on FS5, FS2, FS3 (and FS6 when
    there's no looper); FS1 and FS4 stay free for snapshot up/down. Blocks in
    ``skip`` (the Stage boost) get no switch.
    """
    plan: dict[int, int] = {}
    for index, model in enumerate(models):
        if model.display_name == LOOPER_MODEL:
            plan[index] = LOOPER_SWITCH
    ranked = sorted(
        (i for i, m in enumerate(models) if i not in plan and i not in skip and switch_role(m)),
        key=lambda i: (SWITCH_PRIORITY.index(switch_role(models[i])), i),
    )
    free = [n for n in SWITCH_FILL_ORDER if n not in plan.values()]
    plan.update(zip(ranked, free, strict=False))
    return plan


def switch_label(model: Any) -> str:
    if model.display_name == LOOPER_MODEL:
        return "LOOPER"
    if model.display_name == BOOST_MODEL:
        return "BOOST"
    if model.display_name == CAPO_MODEL:
        return "CAPO"
    if (model.category or "") == WAH_CATEGORY:
        return "WAH"
    return model.display_name[:16]
