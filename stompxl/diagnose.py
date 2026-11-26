"""Read presets off the pedal and describe their footswitch and snapshot data.

For comparing a preset this app wrote against one HX Edit saved: the output
shows the structure (keys, types, lengths) the two differ in, which is what
pins down device-specific layout like the Stomp XL's footswitch numbering.

    uv run stompxl diagnose 29B Test1   # by pedal label or preset name
"""

from __future__ import annotations

import json
from typing import Any

from . import config, pedal, xl


def _text(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return value


def _plain(value: Any) -> Any:
    """Full values with bytes made readable, for small structures."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return _text(value)


def _shape(value: Any, depth: int = 0) -> Any:
    """Keys, types and lengths without the bulk, to compare structures."""
    if depth > 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): _shape(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        if not value:
            return []
        return {"len": len(value), "first": _shape(value[0], depth + 1)}
    if isinstance(value, bytes):
        return f"bytes[{len(value)}]"
    return type(value).__name__


#: Controller sources as read off Stomp XL presets HX Edit saved (EXP 2 by
#: analogy with EXP 1; 11 appeared with a block's bypass on the pedal).
SOURCE_LABELS = {
    1: "EXP 1",
    2: "EXP 2",
    11: "EXP bypass (block engages with the pedal)",
    12: "Snapshots",
    9: "Snapshots (HX Stomp numbering)",
}


def controller_assignments(document: Any, symbols: Any = None) -> list[dict[str, Any]]:
    """Every controller assignment in readable form: what drives which parameter."""
    from stompxl.helixgen.device import document as d

    out = []
    group = document.preset.get(d.KEY_CONTROLLER_GROUP)
    for source, assignments in enumerate(group or []):
        if not isinstance(assignments, list):
            continue
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            body = assignment.get(d.KEY_CTRL_BODY) or {}
            target = body.get(d.KEY_CTRL_TARGET) or {}
            slot = body.get(d.KEY_CTRL_SLOT)
            ordinal = target.get(d.KEY_TARGET_PARAM)
            model_sel = target.get(d.KEY_TARGET_MODEL_SEL, 0)
            index = None
            if isinstance(slot, int):
                index = document.cab_index(slot) if model_sel == 1 else document.model_index(slot)
            model = parameter = None
            symbol = symbols.by_index(index) if symbols is not None and index is not None else None
            if symbol is not None:
                model = symbol.symbol
                if isinstance(ordinal, int) and 0 <= ordinal < len(symbol.parameters):
                    parameter = symbol.parameters[ordinal]
            entry = {
                "source": source,
                "source_label": SOURCE_LABELS.get(source, f"source {source}"),
                "id": assignment.get(d.KEY_CTRL_ID),
                "block_slot": slot,
                "model_index": index,
                "model": model,
                "parameter_ordinal": ordinal,
                "parameter": parameter,
                "min": body.get(d.KEY_CTRL_MIN),
                "max": body.get(d.KEY_CTRL_MAX),
            }
            what = f"{model or f'model #{index}'}.{parameter or f'param #{ordinal}'}"
            entry["summary"] = (f"{entry['source_label']} (source {source}) -> slot {slot} {what} "
                                f"[{entry['min']} .. {entry['max']}]")
            out.append(entry)
    return out


def describe(document: Any, symbols: Any = None) -> dict[str, Any]:
    from stompxl.helixgen.device import document as d

    footswitches = []
    for position, entry in enumerate(document.footswitches()):
        if isinstance(entry, list) and entry and isinstance(entry[0], dict):
            inner = entry[0].get(d.KEY_FS_INNER, {})
            footswitches.append({
                "position": position,
                "fs_index": position + 1,
                "block_slot": inner.get(d.KEY_FS_BLOCK),
                "label": _text(inner.get(d.KEY_FS_LABEL)),
                "enabled": inner.get(d.KEY_FS_ENABLED),
                "assignments": len(entry),
                "entry": {str(k): _text(v) for k, v in entry[0].items() if k != d.KEY_FS_INNER},
                "inner": {str(k): _text(v) for k, v in inner.items()},
            })
        else:
            footswitches.append({"position": position, "fs_index": position + 1, "empty": entry is None})

    snapshots = []
    for index, snap in enumerate(document.snapshots()):
        if not isinstance(snap, dict):
            snapshots.append({"index": index, "raw_type": type(snap).__name__})
            continue
        controllers = snap.get(d.KEY_SNAP_CONTROLLERS)
        used = [c for c in controllers or [] if isinstance(c, list) and len(c) > 2 and c[2] is not None]
        blocks = snap.get(d.KEY_SNAP_BLOCKS)
        snapshots.append({
            "index": index,
            "valid": snap.get(d.KEY_SNAP_VALID),
            "name": _text(snap.get(d.KEY_SNAP_NAME)),
            "tempo": snap.get(d.KEY_SNAP_TEMPO),
            "keys": sorted(str(k) for k in snap),
            "block_states": [b[:2] if isinstance(b, list) else b for b in blocks or []],
            "controller_values_used": used,
            "other": {str(k): _text(v) for k, v in snap.items()
                      if k not in (d.KEY_SNAP_CONTROLLERS, d.KEY_SNAP_BLOCKS)},
        })

    # Input/output/routing nodes, in full: where the Input block's noise gate
    # (on/off, threshold, decay) lives is still to be learned.
    nodes = []
    for position, entry in enumerate(document.slot_array(0) or []):
        if isinstance(entry, dict) and entry.get(d.KEY_TAG) in (d.TAG_INPUT, d.TAG_OUTPUT, d.TAG_ROUTING):
            nodes.append({"position": position, "tag": entry.get(d.KEY_TAG),
                          "content": _plain(entry.get(d.KEY_CONTENT))})

    group = document.preset.get(d.KEY_CONTROLLER_GROUP)
    controllers = {
        str(source): assignments
        for source, assignments in enumerate(group or [])
        if isinstance(assignments, list) and assignments
    }
    snapshot_group = document.preset.get(d.KEY_SNAPSHOT_GROUP) or {}
    return {
        "blocks": [{"slot": slot, "model_index": document.model_index(slot),
                    "enabled": block.get(10) if isinstance(block, dict) else None}
                   for slot, block in document.blocks(0)],
        "footswitches": footswitches,
        "snapshots": snapshots,
        "snapshot_group": {str(k): _text(v) for k, v in snapshot_group.items() if k != d.KEY_SNAPSHOT_ARRAY},
        "controller_assignments": controller_assignments(document, symbols),
        "controllers_by_source": controllers,
        "nodes": nodes,
        "input_gate": document.input_gate(),
        "io_values": {"input": _plain(document.io_values(d.INPUT_RECORD)),
                      "output": _plain(document.io_values(d.OUTPUT_RECORD))},
        "top_level_keys": sorted(str(k) for k in document.preset),
        "shape": {
            "footswitch_group": _shape(document.preset.get(d.KEY_FOOTSWITCH_GROUP)),
            "snapshot_group": _shape(snapshot_group),
            "controller_group": _shape(group),
        },
    }


def deep_diff(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    """Every place two decoded presets differ, by key path (e.g. ``3/9/7/4/0``)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            sub = f"{path}/{key}"
            if key not in a or key not in b:
                out.append({"path": sub, "first": _plain(a.get(key)), "other": _plain(b.get(key))})
            else:
                out.extend(deep_diff(a[key], b[key], sub))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            out.extend(deep_diff(x, y, f"{path}/{index}"))
        return out
    if a != b:
        return [{"path": path or "/", "first": _plain(a), "other": _plain(b)}]
    return []


class SlotNotFound(ValueError):
    pass


def resolve_slot(text: str, names: dict[int, str]) -> int:
    """A slot from what the player sees: "29B", a preset name, or a slot number."""
    value = text.strip()
    label = value.upper()
    if len(label) >= 2 and label[:-1].isdigit() and label[-1] in "ABCD":
        slot = (int(label[:-1]) - 1) * 4 + "ABCD".index(label[-1])
        if 0 <= slot < xl.SLOT_COUNT:
            return slot
    def squash(text: str) -> str:
        return "".join(text.split()).lower()

    matches = [s for s, name in names.items() if squash(name) == squash(value)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SlotNotFound(
            f"Several presets are named '{value}' "
            f"({', '.join(pedal.slot_label(s) for s in matches)}); use the slot label instead."
        )
    if value.isdigit() and 0 <= int(value) < xl.SLOT_COUNT:
        return int(value)
    raise SlotNotFound(f"No slot or preset called '{value}'. Use a label like 29B or the preset's name.")


def _symbols() -> Any:
    """HX Edit's model table, to name models and parameters; None without it."""
    try:
        from stompxl.helixgen.device.hxedit import find_symbol_table
        from stompxl.helixgen.device.symbols import DeviceSymbols

        return DeviceSymbols.load(find_symbol_table(None))
    except Exception:  # noqa: BLE001 - the report is still useful with numbers only
        return None


def run(targets: list[str]) -> tuple[str, list[str]]:
    """Write the report; return its path and a readable summary for the terminal."""
    from stompxl.helixgen.device.document import parse
    from stompxl.helixgen.device.usb import Session

    names = pedal.list_slots()
    slots = [resolve_slot(t, names) for t in targets]
    symbols = _symbols()
    report: dict[str, Any] = {"device": pedal.status().description, "slots": {}}
    lines: list[str] = []
    documents: list[tuple[str, Any]] = []
    with Session.open() as session:
        for slot in slots:
            raw = session.read_slot(slot=slot)
            key = f"{pedal.slot_label(slot)} #{slot} {names.get(slot, '')}".strip()
            if not raw:
                report["slots"][key] = "empty"
                lines.append(f"{key}: empty")
                continue
            document = parse(raw)
            documents.append((key, document.preset))
            described = describe(document, symbols)
            report["slots"][key] = described
            lines.append(f"{key}:")
            gate = described["input_gate"]
            if gate is not None:
                lines.append(f"  input noise gate: {'on' if gate[0] else 'off'}, threshold {gate[1]:g} dB")
            io = described["io_values"]
            lines.append(f"  input values: {io['input']}  output values: {io['output']}")
            assignments = described["controller_assignments"]
            lines.extend(f"  {a['summary']}" for a in assignments)
            if not assignments:
                lines.append("  no controller assignments (no EXP pedal or snapshot control)")
    # Everything that differs from the first preset, anywhere in the document:
    # finds settings the summary above doesn't know to look at.
    if len(documents) > 1:
        first_key, first = documents[0]
        report["differences"] = {}
        for key, other in documents[1:]:
            diffs = deep_diff(first, other)
            report["differences"][f"{first_key} vs {key}"] = diffs
            lines.append(f"{first_key} vs {key}: {len(diffs)} difference(s)")
            lines.extend(f"  {d['path']}: {d['first']!r} -> {d['other']!r}" for d in diffs[:20])
    out = config.data_dir() / "diagnose.json"
    out.write_text(json.dumps(report, indent=1, default=repr))
    return str(out), lines
