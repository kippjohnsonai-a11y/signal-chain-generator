"""A preset as the UI shows it: blocks with readable values, and per-snapshot
differences, derived from the assembled .hlx rather than the chain spec so it
shows exactly what reaches the pedal."""

from __future__ import annotations

from typing import Any

from stompxl.helixgen.dataset import CONTINUOUS, DISCRETE

from . import engine, xl

_CONTROLLERS = {9: "snapshot", xl.CONTROLLER_SNAPSHOT: "snapshot", 1: "EXP 1", 2: "EXP 2"}


def display_value(definition: Any, value: Any) -> str:
    if value is None:
        return "-"
    if definition.display_type == "boolean" or isinstance(value, bool):
        return "On" if value else "Off"
    if definition.forward_map:
        return str(definition.forward_map.get(str(value), value))
    if isinstance(value, (int, float)):
        scale = definition.raw.get("scale")
        if definition.value_type == CONTINUOUS and scale:
            return f"{value * scale:.1f}"
        return f"{value:.1f}" if isinstance(value, float) else str(value)
    return str(value)


def is_default(definition: Any, value: Any) -> bool:
    """Whether ``value`` is the model's own default (so not worth featuring)."""
    try:
        default = definition.default_value()
    except Exception:  # noqa: BLE001 - an odd dataset default just means "not default"
        return False
    if default is None:
        return False
    if isinstance(default, (int, float)) and isinstance(value, (int, float)):
        return abs(float(default) - float(value)) < 1e-6
    return default == value


def label(name: str, definition: Any) -> str:
    """The parameter's name as HX Edit shows it ("Shift Level", not "LevelVoice1")."""
    if name == xl.PEDAL_PARAMETER:
        return "Position"
    return definition.raw.get("parameter") or name


def snapshot_editable(name: str, definition: Any) -> bool:
    """Whether a snapshot can hold its own value for this parameter."""

    return (definition.controller_range() is not None and name != xl.PEDAL_PARAMETER
            and not xl.is_stereo_only(name))


def editor(name: str, definition: Any) -> dict[str, Any]:
    """How the UI edits a parameter, in the units it displays."""

    if definition.display_type == "boolean":
        return {"kind": "toggle"}
    if definition.forward_map:
        return {"kind": "choice", "options": [str(v) for v in definition.forward_map.values()]}
    out: dict[str, Any] = {"kind": "number"}
    span = definition.controller_range()
    if span is None and definition.min_value is not None and definition.max_value is not None:
        span = (definition.min_value, definition.max_value)
    if span is not None:
        scale = definition.raw.get("scale") if definition.value_type == CONTINUOUS else None
        low, high = (v * scale for v in span) if scale else span
        width = high - low
        out.update(min=round(low, 3), max=round(high, 3),
                   step=1 if definition.value_type == DISCRETE else 0.01 if width <= 1 else 0.1 if width <= 200 else 1)
    return out


def _expression(spec: dict[str, Any], model: Any) -> dict[str, Any]:
    """What the block's expression pedal moves, for the block editor."""
    if not spec.get("expression"):
        return {}
    param = spec.get("expression_param") or xl.PEDAL_PARAMETER
    if param not in model.parameters:
        return {}
    definition = model.get_parameter(param)
    span = spec.get("expression_range")
    out: dict[str, Any] = {"expression_param": param,
                           "expression_label": label(param, definition)}
    if span:
        out["expression_heel"] = display_value(definition, span[0])
        out["expression_toe"] = display_value(definition, span[1])
    return out


def io_display(spec: tuple, value: Any) -> str:
    key, _label, kind, _span, scale = spec
    if kind == "toggle":
        return "On" if value else "Off"
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value * scale:.1f}" if key != "threshold" else f"{value:g}"


def io_blocks(tone: dict[str, Any]) -> list[dict[str, Any]]:
    """The Input and Output as editable cards, from what the preset holds."""
    dsp0 = tone.get("dsp0", {})
    out = []
    for io, node in (("input", "inputA"), ("output", "outputA")):
        payload = {**xl.IO_DEFAULTS[io], **(dsp0.get(node) or {})}
        params = []
        for spec in xl.IO_PARAMS[io]:
            key, label_text, kind, span, _scale = spec
            edit: dict[str, Any] = {"kind": kind, "per_snapshot": False}
            if span:
                edit.update(min=span[0], max=span[1], step=span[2])
            params.append({"name": label_text, "param": key,
                           "value": io_display(spec, payload.get(key)),
                           "controlled_by": None, "changed": False, "edit": edit})
        out.append({"key": io, "io": io, "model": xl.IO_MODELS[io], "title": xl.IO_TITLES[io],
                    "category": "Routing", "params": params,
                    "why": "Gate and level at the pedal's input." if io == "input"
                    else "Overall preset level and pan at the main output."})
    return out


def _playing(tone: dict[str, Any]) -> int:
    current = (tone.get("global") or {}).get("@current_snapshot", 0)
    return current if isinstance(current, int) and f"snapshot{current}" in tone else 0


def preset_view(chain: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    cat = engine.catalog()
    tone = preset["data"]["tone"]
    dsp0 = tone["dsp0"]
    controllers = tone.get("controller", {}).get("dsp0", {})
    footswitches = tone.get("footswitch", {}).get("dsp0", {})

    blocks = []
    for index, spec in enumerate(chain.get("blocks", [])):
        key = f"block{index}"
        model = cat.get(spec["model"])
        payload = dsp0.get(key, {})
        nam = bool(spec.get("nam"))
        params = []
        for name in sorted(model.parameter_names()):
            if name.startswith("@"):
                continue
            definition = model.get_parameter(name)
            control = controllers.get(key, {}).get(name, {}).get("@controller")
            params.append({
                "name": xl.NAM_KNOBS.get(name, name) if nam else label(name, definition),
                "param": name,
                "value": display_value(definition, payload.get(name)),
                "controlled_by": _CONTROLLERS.get(control),
                "changed": (name in (spec.get("parameters") or {})
                            and not is_default(definition, spec["parameters"][name])),
                "edit": {**editor(name, definition),
                         "per_snapshot": snapshot_editable(name, definition)},
            })
        fs = footswitches.get(key)
        blocks.append({
            "key": key,
            "index": index,
            "enabled": payload.get("@enabled", True) is not False,
            # The boost and looper are stomps: only their footswitch turns them on.
            "stomp": model.display_name in (xl.BOOST_MODEL, xl.LOOPER_MODEL)
            or bool(spec.get("stage_boost")),
            "stage_boost": bool(spec.get("stage_boost")),
            "model": f"{xl.NAM_TITLE} (HX Edit: {model.display_name})" if nam else model.display_name,
            "nam": nam,
            #: The catalog name, which quick picks for one model go by.
            "model_key": model.display_name,
            "path": payload.get("@path", 0),
            "category": model.category,
            "based_on": model.based_on,
            "why": spec.get("why", ""),
            "footswitch": spec.get("switch") or (fs.get("@fs_index") if fs else None),
            "expression": spec.get("expression"),
            **_expression(spec, model),
            "params": params,
        })

    snapshots = []
    for index in range(4):
        snap = tone.get(f"snapshot{index}")
        if not isinstance(snap, dict):
            continue
        states = snap.get("blocks", {}).get("dsp0", {})
        values = snap.get("controllers", {}).get("dsp0", {})
        changes: dict[str, list[dict[str, str]]] = {}
        for key, params in values.items():
            position = int(key[5:])
            if position >= len(chain["blocks"]):
                continue
            model = cat.get(chain["blocks"][position]["model"])
            changes[key] = [
                {"name": label(name, model.get_parameter(name)), "param": name,
                 "value": display_value(model.get_parameter(name), entry.get("@value"))}
                for name, entry in params.items()
                if name in model.parameters
            ]
        snapshots.append({
            "name": snap.get("@name"),
            "enabled": {k: bool(v) for k, v in states.items()},
            "values": changes,
        })
    dsp = xl.dsp_total([cat.get(spec["model"]) for spec in chain.get("blocks", [])])
    return {"blocks": blocks, "snapshots": snapshots, "io": io_blocks(tone),
            "dsp_estimate": round(dsp),
            "tempo": tone.get("global", {}).get("@tempo"),
            # The snapshot the preset opens on: what the pedal plays (and Live edits).
            "playing_snapshot": _playing(tone),
            "nam": chain.get("nam")}
