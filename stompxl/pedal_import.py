"""Read a preset off the pedal into the .hlx shape the importer takes.

The device document is decoded with the same keys the push writes: blocks by
slot (model index into Helix.sym, values in that symbol's parameter order,
an amp's paired cab alongside), snapshots (names, per-slot on/off, and a value
per controller assignment), controller assignments (EXP and Snapshots),
footswitches, and the Input and main Output records. Nothing is written back.
"""

from __future__ import annotations

from typing import Any

from stompxl.helixgen.device import document as d
from stompxl.helixgen.device.symbols import normalize_parameter_name

from . import xl


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return str(value or "")


def _block_key(index: int) -> tuple[str, int, int]:
    """Slot index -> (.hlx key, path, position): slots 1-8 are path A, 11-18 path B."""
    path, position = divmod(index, d.PATH_STRIDE)
    position -= 1
    return f"block{path * d.PATH_STRIDE + position}", path, position


def _named(symbol: Any, values: list | None, model: Any) -> dict[str, Any]:
    """A value vector as ``{catalog name: value}``, matched through Helix.sym."""
    if symbol is None or values is None:
        return {}
    ours = {normalize_parameter_name(n): n for n in model.parameters} if model else {}
    out = {}
    for name, value in zip(symbol.parameters, values, strict=False):
        out[ours.get(normalize_parameter_name(name), name)] = value
    return out


def document_to_hlx(document: Any, symbols: Any, catalog: Any, name: str = "") -> dict[str, Any]:
    dsp0: dict[str, Any] = {}
    keys: dict[tuple[int, int], tuple[str, Any]] = {}  # (slot, model_sel) -> (.hlx key, symbol)
    for index, content in document.blocks(0):
        key, path, position = _block_key(index)
        model_index = document.model_index(index)
        symbol = symbols.by_index(model_index) if model_index is not None else None
        if symbol is None:
            dsp0[key] = {"@model": f"model #{model_index}", "@path": path, "@position": position}
            continue
        model = xl.model_for_symbol(symbol.symbol, catalog)
        block = {"@model": symbol.symbol, "@path": path, "@position": position,
                 "@enabled": content.get(d.KEY_ENABLED) is not False,
                 **_named(symbol, document.values(index), model)}
        cab_index = document.cab_index(index)
        cab_symbol = symbols.by_index(cab_index) if cab_index is not None else None
        if cab_symbol is not None:
            cab_key = f"cab{path * d.PATH_STRIDE + position}"
            cab_model = xl.model_for_symbol(cab_symbol.symbol, catalog)
            dsp0[cab_key] = {"@model": cab_symbol.symbol,
                             **_named(cab_symbol, document.values(index, cab=True), cab_model)}
            block["@cab"] = cab_key
            keys[(index, 1)] = (cab_key, cab_symbol)
        dsp0[key] = block
        keys[(index, 0)] = (key, symbol)

    tone: dict[str, Any] = {"dsp0": dsp0}
    _io(document, symbols, dsp0)

    # Snapshots: names, tempo, per-slot on/off.
    snapshots = document.snapshots()
    for number, snap in enumerate(snapshots):
        if not isinstance(snap, dict):
            continue
        states = snap.get(d.KEY_SNAP_BLOCKS) or []
        on = {}
        for (slot, sel), (key, _) in keys.items():
            if sel == 0 and slot < len(states) and isinstance(states[slot], list) and len(states[slot]) > 1:
                on[key] = bool(states[slot][1])
        tone[f"snapshot{number}"] = {
            "@name": _text(snap.get(d.KEY_SNAP_NAME)) or f"SNAPSHOT {number + 1}",
            "@valid": snap.get(d.KEY_SNAP_VALID) is not False,
            "@tempo": snap.get(d.KEY_SNAP_TEMPO),
            "blocks": {"dsp0": on},
            "controllers": {"dsp0": {}},
        }

    # Controller assignments: EXP pedals, and the Snapshots source's per-snapshot values.
    group = document.preset.get(d.KEY_CONTROLLER_GROUP)
    controller: dict[str, Any] = {}
    for source, assignments in enumerate(group if isinstance(group, list) else []):
        for assignment in assignments if isinstance(assignments, list) else []:
            body = assignment.get(d.KEY_CTRL_BODY) if isinstance(assignment, dict) else None
            if not isinstance(body, dict):
                continue
            target = body.get(d.KEY_CTRL_TARGET) or {}
            found = keys.get((body.get(d.KEY_CTRL_SLOT), target.get(d.KEY_TARGET_MODEL_SEL, 0)))
            ordinal = target.get(d.KEY_TARGET_PARAM)
            if found is None or not isinstance(ordinal, int):
                continue
            key, symbol = found
            if not 0 <= ordinal < len(symbol.parameters):
                continue
            model = xl.model_for_symbol(symbol.symbol, catalog)
            names = _named(symbol, [None] * len(symbol.parameters), model)
            param = list(names)[ordinal]
            if source in d.EXPRESSION_SOURCES:
                controller.setdefault(key, {})[param] = {
                    "@controller": source, "@min": body.get(d.KEY_CTRL_MIN),
                    "@max": body.get(d.KEY_CTRL_MAX)}
            elif source in d.SNAPSHOT_SOURCES:
                ident = assignment.get(d.KEY_CTRL_ID)
                for number, snap in enumerate(snapshots):
                    values = snap.get(d.KEY_SNAP_CONTROLLERS) if isinstance(snap, dict) else None
                    if (isinstance(values, list) and isinstance(ident, int) and ident < len(values)
                            and isinstance(values[ident], list) and len(values[ident]) > 2
                            and values[ident][2] is not None and f"snapshot{number}" in tone):
                        tone[f"snapshot{number}"]["controllers"]["dsp0"].setdefault(key, {})[param] = {
                            "@value": values[ident][2]}
    if controller:
        tone["controller"] = {"dsp0": controller}

    # Footswitches: the first assignment on each switch that drives a block.
    by_slot = {slot: key for (slot, sel), (key, _) in keys.items() if sel == 0}
    footswitch: dict[str, Any] = {}
    for position, entry in enumerate(document.footswitches()):
        if not (isinstance(entry, list) and entry and isinstance(entry[0], dict)):
            continue
        inner = entry[0].get(d.KEY_FS_INNER) or {}
        key = by_slot.get(inner.get(d.KEY_FS_BLOCK))
        if key and key not in footswitch:
            footswitch[key] = {"@fs_index": position + 1, "@fs_label": _text(inner.get(d.KEY_FS_LABEL))}
    if footswitch:
        tone["footswitch"] = {"dsp0": footswitch}

    group_meta = document.preset.get(d.KEY_SNAPSHOT_GROUP) or {}
    current = group_meta.get(d.KEY_CURRENT_SNAPSHOT, 0) if isinstance(group_meta, dict) else 0
    tempo = (tone.get(f"snapshot{current}") or tone.get("snapshot0") or {}).get("@tempo")
    tone["global"] = {"@tempo": float(tempo) if isinstance(tempo, (int, float)) else 120.0}
    return {"data": {"device": xl.DEVICE_ID, "meta": {"name": name}, "tone": tone,
                     "device_settings": _settings(document)}}


#: Preset-level sections nobody has decoded; carried over verbatim so an
#: imported preset sent back to the pedal keeps them (e.g. 5/30, 5/49, 6/98).
_SETTINGS_SECTIONS = (5, 6)


def _settings(document: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for section in _SETTINGS_SECTIONS:
        values = document.preset.get(section)
        if isinstance(values, dict):
            out[str(section)] = {str(k): v for k, v in values.items()
                                 if isinstance(k, int) and isinstance(v, (bool, int, float))}
    return out


def _io(document: Any, symbols: Any, dsp0: dict[str, Any]) -> None:
    """The Input gate and main Output, where their layout is the one confirmed."""
    gate = document.input_gate()
    if gate is not None:
        section: dict[str, Any] = {"noiseGate": gate[0], "threshold": gate[1]}
        entry = symbols.by_symbol("HelixStomp_AppDSPFlowInput")
        values = document.io_values(d.INPUT_RECORD) or []
        if (entry and entry.ordinal_of("noiseGate") == 0 and entry.ordinal_of("threshold") == 1
                and (ordinal := entry.ordinal_of("decay")) is not None and ordinal < len(values)
                and isinstance(values[ordinal], (int, float)) and not isinstance(values[ordinal], bool)):
            section["decay"] = float(values[ordinal])
        dsp0["inputA"] = section
    join = symbols.by_symbol("HD2_AppDSPFlowJoin")
    values = document.join_values() or []
    if join is not None and values:
        mix = {name: value for name, value in zip(join.parameters, values, strict=False)
               if isinstance(value, (int, float))}
        if mix:
            dsp0["join"] = mix
    entry = symbols.by_symbol("HelixStomp_AppDSPFlowOutputMain")
    values = document.io_values(d.OUTPUT_RECORD) or []
    if entry is not None:
        out = {}
        for key in ("gain", "pan"):
            ordinal = entry.ordinal_of(key)
            if (ordinal is not None and ordinal < len(values) and isinstance(values[ordinal], (int, float))
                    and not isinstance(values[ordinal], bool)):
                out[key] = float(values[ordinal])
        if out:
            dsp0["outputA"] = out
