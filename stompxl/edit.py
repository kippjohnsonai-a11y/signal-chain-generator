"""Hand edits: change values, blocks, footswitches and expression directly,
no Claude call.

Edits arrive in display units (Drive 6.5, Mix 30 %, a delay's "1/8 dot") and
land in the chain spec as the raw numbers the preset stores. A value edited for
one snapshot becomes snapshot-controlled, the other snapshots keeping theirs.
Edits apply in order, so a block index always means the chain as the edits
before it left it.
"""

from __future__ import annotations

import copy
from typing import Any

from stompxl.helixgen.dataset import CONTINUOUS, ModelCatalogError, ParameterDefinition

from . import config, engine, xl
from .view import display_value, label, snapshot_editable
from .view import io_display as view_io

#: HX Edit's label for the expression sweep, which the view shows.
_ALIASES = {"Position": xl.PEDAL_PARAMETER}


class EditError(ValueError):
    pass


def to_raw(definition: ParameterDefinition, value: Any) -> Any:
    """A display value as the number the preset stores."""

    if definition.value_type == CONTINUOUS and not definition.forward_map:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise EditError(f"{definition.name} needs a number") from exc
        scale = definition.raw.get("scale")
        if scale:
            numeric /= scale
        return definition.normalize(numeric)
    try:
        return definition.normalize(value)
    except ModelCatalogError as exc:
        raise EditError(str(exc)) from exc


def apply_edits(chain: dict[str, Any], preset: dict[str, Any] | None,
                edits: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """``chain`` with ``edits`` applied, and a readable line per change.

    Each edit is one of:

    - ``{block, parameter, value, snapshot?}``: a value, in display units;
    - ``{block, enabled, snapshot?}``: switch a block on or off;
    - ``{block, model}``: swap in a different model;
    - ``{insert, model}``: add a block at that position;
    - ``{remove}``: take the block at that position out;
    - ``{move, to}``: move the block at ``move`` so it ends up at ``to``;
    - ``{block, switch}``: put the block on footswitch 1-6, or none;
    - ``{block, expression, parameter?, heel?, toe?}``: put a parameter
      (default the sweep) on EXP 1 or 2, or none, optionally over a range;
    - ``{io, parameter, value}``: the Input (gate) or Output (level, pan);
    - ``{tempo}``: the preset's tempo in BPM;
    - ``{snapshot_name, name}``: rename snapshot ``snapshot_name`` (an index);
    - ``{stage_boost: true}``: add the Stage boost (off) just before the amp;
    - ``{block, nam}``: say a Xenomorph Fuzz is the NAM A2 Lite block (true)
      or a real fuzz (false). The pedal plays the same either way; it changes
      how the app shows and treats it.
    """

    del preset  # values are read off the chain, which tracks every edit
    cat = engine.catalog()
    chain = copy.deepcopy(chain)
    blocks = chain.setdefault("blocks", [])
    snapshots = chain.setdefault("snapshots", [])
    changes: list[str] = []

    for edit in edits:
        # Adding or removing a block rebuilds the snapshot list.
        snapshots = chain["snapshots"]
        if "tempo" in edit:
            changes.append(_set_tempo(chain, edit["tempo"]))
            continue
        if "snapshot_name" in edit:
            changes.append(_name_snapshot(snapshots, edit["snapshot_name"], edit.get("name")))
            continue
        if "io" in edit:
            changes.append(_set_io(chain, edit))
            continue
        if "insert" in edit:
            changes.append(_insert(chain, edit["insert"], edit.get("model"), cat))
            continue
        if edit.get("stage_boost"):
            changes.append(_stage_boost(chain, cat))
            continue
        if "remove" in edit:
            changes.append(_remove(chain, edit["remove"], cat))
            continue
        if "move" in edit:
            changes.append(_move(chain, edit["move"], edit.get("to"), cat))
            continue

        index = edit.get("block")
        if not isinstance(index, int) or not 0 <= index < len(blocks):
            raise EditError(f"No block {index}")
        block = blocks[index]
        model = cat.get(block["model"])

        if "model" in edit:
            changes.append(_swap(chain, index, edit["model"], cat))
            continue
        if "switch" in edit:
            changes.append(_set_switch(blocks, index, edit["switch"], model))
            continue
        if "expression" in edit:
            changes.append(_set_expression(blocks, index, edit, model))
            continue
        if "nam" in edit:
            changes.append(_set_nam(chain, index, bool(edit["nam"]), model))
            continue

        snapshot = edit.get("snapshot")
        where = ""
        target: dict[str, Any] = block
        if snapshot is not None:
            if not isinstance(snapshot, int) or not 0 <= snapshot < len(snapshots):
                raise EditError(f"No snapshot {snapshot}")
            snap = snapshots[snapshot]
            where = f" ({snap.get('name') or f'snapshot {snapshot + 1}'})"
            target = snap.setdefault("blocks", {}).setdefault(str(index), {})

        if edit.get("parameter"):
            name = _parameter(model, edit["parameter"])
            definition = model.get_parameter(name)
            if snapshot is not None and not snapshot_editable(name, definition):
                raise EditError(f"{model.display_name} {label(name, definition)} can't change per "
                                "snapshot; edit it for all snapshots")
            raw = to_raw(definition, edit.get("value"))
            before = _current(chain, index, name, snapshot, definition)
            target.setdefault("parameters", {})[name] = raw
            changes.append(f"{model.display_name} {label(name, definition)}{where}: "
                           f"{display_value(definition, before)} -> "
                           f"{display_value(definition, raw)}")
        elif "enabled" in edit:
            on = bool(edit["enabled"])
            if snapshot is not None and model.display_name in (xl.BOOST_MODEL, xl.LOOPER_MODEL):
                raise EditError(f"The {model.display_name} is a stomp: its footswitch turns it "
                                "on, not the snapshots")
            target["enabled"] = on
            changes.append(f"{model.display_name}{where}: {'on' if on else 'off'}")
        else:
            raise EditError("An edit needs a parameter and value, or enabled")

    for snap in chain["snapshots"]:
        for key in [k for k, v in (snap.get("blocks") or {}).items() if not v]:
            del snap["blocks"][key]
    return chain, changes


def _set_nam(chain: dict[str, Any], index: int, on: bool, model: Any) -> str:
    if model.display_name != xl.NAM_MODEL:
        raise EditError(f"Only a {xl.NAM_MODEL} block can be the {xl.NAM_TITLE} block")
    if (chain.get("nam") or {}).get("mode") == "usb":
        raise EditError("This preset runs NAM on the computer")
    block = chain["blocks"][index]
    if on:
        block["nam"] = True
    else:
        block.pop("nam", None)
    captures = xl.parse_captures(config.get_setting("nam_captures"))
    entry = xl.nam_entry(chain["blocks"], captures)
    if entry is None:
        chain.pop("nam", None)
    elif not chain.get("nam") or not on:
        chain["nam"] = entry
    return f"Block {index + 1}: {xl.NAM_TITLE}" if on else f"Block {index + 1}: {xl.NAM_MODEL}"


def dsp_warning(chain: dict[str, Any]) -> str:
    """A note when the edited chain is likely past what the pedal holds."""
    cat = engine.catalog()
    total = xl.dsp_total([cat.get(b["model"]) for b in chain.get("blocks", [])])
    budget = xl.dsp_budget(config.read_settings())
    if total > budget:
        return (f"DSP estimate ~{total:.0f}% is over what this pedal has held (~{budget:.0f}%); "
                "the pedal may refuse a block when you send it.")
    return ""


def _parameter(model: Any, requested: str) -> str:
    name = _ALIASES.get(requested, requested)
    if name.startswith("@") or name not in model.parameters:
        raise EditError(f"{model.display_name} has no parameter {requested}")
    return name


def _model(cat: Any, name: Any) -> Any:
    if not isinstance(name, str) or not cat.has_model(name):
        raise EditError(f"No model called {name!r}")
    return cat.get(name)


def _fresh_block(model: Any, why: str) -> dict[str, Any]:
    block: dict[str, Any] = {"model": model.display_name, "why": why, "parameters": {}}
    if model.display_name == xl.BOOST_MODEL:
        block["parameters"] = {"Gain": xl.BOOST_DB}
    if model.display_name in (xl.BOOST_MODEL, xl.LOOPER_MODEL):
        # A stomp the snapshots leave alone (see engine._finish_chain).
        block["no_snapshot_bypass"] = True
    return block


def _auto_expression(blocks: list[dict[str, Any]], index: int, model: Any) -> str:
    """Give a wah, whammy or volume pedal its expression pedal if that's free."""
    want = xl.default_expression(model)
    if want is None:
        return ""
    single = (config.get_setting("exp_pedal") or xl.DEFAULT_EXP_PEDAL) == "single"
    pedal = 1 if single else want
    if any(b.get("expression") == pedal for i, b in enumerate(blocks) if i != index):
        return f"; EXP {pedal} is already in use, so its sweep has no pedal"
    blocks[index]["expression"] = pedal
    return f"; sweep on EXP {pedal}"


def _swap(chain: dict[str, Any], index: int, name: Any, cat: Any) -> str:
    blocks = chain["blocks"]
    old_block = blocks[index]
    old = cat.get(old_block["model"])
    new = _model(cat, name)
    block = _fresh_block(new, f"Swapped in by hand for the {old.display_name}.")
    # Carry what the two models share (Mix, Level, Time...).
    block["parameters"].update({k: v for k, v in (old_block.get("parameters") or {}).items()
                                if k in new.parameters})
    if "enabled" in old_block:
        block["enabled"] = old_block["enabled"]
    if "switch" in old_block:
        block.update(switch=old_block["switch"], footswitch=old_block["footswitch"],
                     footswitch_label=xl.switch_label(new))
    param = old_block.get("expression_param") or xl.PEDAL_PARAMETER
    if old_block.get("expression") and param in new.parameters:
        for key in ("expression", "expression_param", "expression_range"):
            if key in old_block:
                block[key] = old_block[key]
    blocks[index] = block
    note = "" if block.get("expression") else _auto_expression(blocks, index, new)
    for snap in chain.get("snapshots") or []:
        change = (snap.get("blocks") or {}).get(str(index))
        if not change:
            continue
        if block.get("no_snapshot_bypass"):
            change.pop("enabled", None)
        params = {k: v for k, v in (change.get("parameters") or {}).items()
                  if k in new.parameters and snapshot_editable(k, new.get_parameter(k))}
        if params:
            change["parameters"] = params
        else:
            change.pop("parameters", None)
    return f"Block {index + 1}: {old.display_name} -> {new.display_name}{note}"


def _insert(chain: dict[str, Any], position: Any, name: Any, cat: Any) -> str:
    blocks = chain["blocks"]
    if len(blocks) >= xl.MAX_BLOCKS:
        raise EditError(f"The chain already has {xl.MAX_BLOCKS} blocks, the most the pedal takes")
    if not isinstance(position, int) or not 0 <= position <= len(blocks):
        raise EditError(f"Can't add a block at position {position}")
    model = _model(cat, name)
    blocks.insert(position, _fresh_block(model, "Added by hand."))
    chain["snapshots"] = engine._remap_snapshots(
        chain.get("snapshots") or [],
        {i: (i if i < position else i + 1) for i in range(len(blocks) - 1)})
    note = _auto_expression(blocks, position, model)
    return f"Added {model.display_name} as block {position + 1}{note}"


def _stage_boost(chain: dict[str, Any], cat: Any) -> str:
    blocks = chain["blocks"]
    if any(b.get("stage_boost") for b in blocks):
        raise EditError("This preset already has a Stage boost")
    if len(blocks) >= xl.MAX_BLOCKS:
        raise EditError(f"The chain already has {xl.MAX_BLOCKS} blocks, so there's no room "
                        "for a Stage boost")
    at = engine.stage_boost_at(blocks, [cat.get(b["model"]) for b in blocks])
    if at is None:
        raise EditError("The Stage boost goes before the amp, and this preset has no amp")
    model = cat.get(xl.STAGE_BOOST_MODEL)
    blocks.insert(at, {"model": model.display_name, "why": engine.STAGE_BOOST_WHY,
                       "parameters": {}, "enabled": False, "stage_boost": True,
                       "no_snapshot_bypass": True})
    chain["snapshots"] = engine._remap_snapshots(
        chain.get("snapshots") or [],
        {i: (i if i < at else i + 1) for i in range(len(blocks) - 1)})
    return f"Added the Stage boost ({model.display_name}, off) as block {at + 1}"


def _remove(chain: dict[str, Any], position: Any, cat: Any) -> str:
    blocks = chain["blocks"]
    if not isinstance(position, int) or not 0 <= position < len(blocks):
        raise EditError(f"No block {position}")
    if len(blocks) == 1:
        raise EditError("A preset needs at least one block")
    removed = blocks.pop(position)
    chain["snapshots"] = engine._remap_snapshots(
        chain.get("snapshots") or [],
        {i: (i if i < position else i - 1) for i in range(len(blocks) + 1) if i != position})
    return f"Removed {cat.get(removed['model']).display_name} (block {position + 1})"


def _move(chain: dict[str, Any], position: Any, to: Any, cat: Any) -> str:
    blocks = chain["blocks"]
    count = len(blocks)
    if not isinstance(position, int) or not 0 <= position < count:
        raise EditError(f"No block {position}")
    if not isinstance(to, int) or not 0 <= to < count:
        raise EditError(f"Can't move a block to position {to}")
    name = cat.get(blocks[position]["model"]).display_name
    usb = (chain.get("nam") or {}).get("mode") == "usb"
    if usb and xl.RETURN_MODEL in (blocks[position]["model"], blocks[to]["model"]):
        raise EditError(f"The {xl.RETURN_MODEL} block brings the guitar in, so it stays first")
    if to == position:
        return f"{name} stays block {position + 1}"
    models = [cat.get(b["model"]) for b in blocks]
    split = xl.nam_split(blocks, models)
    if usb:
        # The paths split at the first block past NAM (xl.nam_split): pin every
        # block's side, then give the moved one the side it lands on. Dropped
        # right at the split, it keeps its own.
        for i, other in enumerate(blocks):
            if other["model"] != xl.RETURN_MODEL:
                other["amp_side"] = "before" if i < split else "after"
    order = list(range(count))
    order.insert(to, order.pop(position))
    block = blocks.pop(position)
    blocks.insert(to, block)
    if usb:
        rest_split = split - (1 if position < split else 0)
        if to < rest_split:
            block["amp_side"] = "before"
        elif to > rest_split:
            block["amp_side"] = "after"
    chain["snapshots"] = engine._remap_snapshots(
        chain.get("snapshots") or [], {old: new for new, old in enumerate(order)})
    return f"Moved {name} to block {to + 1}"


def _set_switch(blocks: list[dict[str, Any]], index: int, switch: Any, model: Any) -> str:
    block = blocks[index]
    if switch in (None, 0, ""):
        for key in ("switch", "footswitch", "footswitch_label"):
            block.pop(key, None)
        return f"{model.display_name}: no footswitch"
    if not isinstance(switch, int) or not 1 <= switch <= xl.FOOTSWITCH_COUNT:
        raise EditError(f"Footswitches run FS1-FS{xl.FOOTSWITCH_COUNT}")
    moved = ""
    for i, other in enumerate(blocks):
        if i != index and other.get("switch") == switch:
            for key in ("switch", "footswitch", "footswitch_label"):
                other.pop(key, None)
            moved = f" (taken from the {other['model']})"
    block.update(switch=switch, footswitch=xl.switch_map(config.read_settings())[switch],
                 footswitch_label=xl.switch_label(model))
    return f"{model.display_name}: FS{switch}{moved}"


def _set_expression(blocks: list[dict[str, Any]], index: int, edit: dict[str, Any],
                    model: Any) -> str:
    block = blocks[index]
    pedal = edit.get("expression")
    if pedal in (None, 0, ""):
        for key in ("expression", "expression_param", "expression_range"):
            block.pop(key, None)
        return f"{model.display_name}: no expression pedal"
    if pedal not in (1, 2):
        raise EditError("Expression pedal is EXP 1 or EXP 2")
    if edit.get("parameter"):
        name = _parameter(model, edit["parameter"])
    elif xl.PEDAL_PARAMETER in model.parameters:
        name = xl.PEDAL_PARAMETER
    else:
        raise EditError(f"Pick which {model.display_name} parameter the pedal moves")
    definition = model.get_parameter(name)
    span = definition.controller_range()
    if span is None:
        raise EditError(f"{model.display_name} {label(name, definition)} can't be on a pedal")
    heel = edit.get("heel")
    toe = edit.get("toe")
    low = to_raw(definition, heel) if heel not in (None, "") else span[0]
    high = to_raw(definition, toe) if toe not in (None, "") else span[1]
    block["expression"] = pedal
    if name == xl.PEDAL_PARAMETER:
        block.pop("expression_param", None)
    else:
        block["expression_param"] = name
    if (low, high) == tuple(span):
        block.pop("expression_range", None)
    else:
        block["expression_range"] = [low, high]
    return (f"{model.display_name} {label(name, definition)} on EXP {pedal} "
            f"(heel {display_value(definition, low)}, toe {display_value(definition, high)})")


def _set_tempo(chain: dict[str, Any], tempo: Any) -> str:
    try:
        bpm = float(tempo)
    except (TypeError, ValueError) as exc:
        raise EditError("Tempo needs a number") from exc
    if not 30 <= bpm <= 300:
        raise EditError("Tempo runs 30-300 BPM")
    section = chain.setdefault("global", {})
    before = section.get("@tempo", 120.0)
    section["@tempo"] = bpm
    return f"Tempo: {before:g} -> {bpm:g} BPM"


def _name_snapshot(snapshots: list[dict[str, Any]], index: Any, name: Any) -> str:
    if not isinstance(index, int) or not 0 <= index < len(snapshots):
        raise EditError(f"No snapshot {index}")
    clean = "".join(ch for ch in str(name or "") if 32 <= ord(ch) < 127).strip()
    clean = clean[: xl.SNAPSHOT_NAME_LIMIT]
    if not clean:
        raise EditError("A snapshot name needs at least one letter or number")
    before = snapshots[index].get("name") or f"SNAPSHOT {index + 1}"
    snapshots[index]["name"] = clean
    return f"Snapshot {index + 1}: {before} -> {clean}"


def _set_io(chain: dict[str, Any], edit: dict[str, Any]) -> str:
    io = edit.get("io")
    specs = {spec[0]: spec for spec in xl.IO_PARAMS.get(io, [])}
    spec = specs.get(edit.get("parameter"))
    if spec is None:
        raise EditError(f"The {io} has no setting {edit.get('parameter')!r}")
    key, label_text, kind, span, scale = spec
    value = edit.get("value")
    if kind == "toggle":
        raw: Any = value if isinstance(value, bool) else str(value).strip().lower() in ("on", "true", "1")
    else:
        try:
            shown = float(value)
        except (TypeError, ValueError) as exc:
            raise EditError(f"{xl.IO_TITLES[io]} {label_text} needs a number") from exc
        if span:
            shown = max(span[0], min(span[1], shown))
        raw = shown / scale
    node = chain.get(io)
    section = {**xl.IO_DEFAULTS[io], **(node if isinstance(node, dict) else {})}
    before = section.get(key)
    section[key] = raw
    chain[io] = section
    if io == "input":
        # The version header and Claude's next tweak read the gate from here.
        chain["gate"] = {"enabled": bool(section["noiseGate"]),
                         "threshold_db": float(section["threshold"]), "why": "Set by hand."}
    return (f"{xl.IO_TITLES[io]} {label_text}: {view_io(spec, before)} -> {view_io(spec, raw)}")


def _current(chain: dict[str, Any], index: int, name: str, snapshot: int | None,
             definition: Any) -> Any:
    if snapshot is not None:
        snaps = chain.get("snapshots") or []
        change = (snaps[snapshot].get("blocks") or {}).get(str(index)) or {}
        if name in (change.get("parameters") or {}):
            return change["parameters"][name]
    params = chain["blocks"][index].get("parameters") or {}
    if name in params:
        return params[name]
    return definition.default_value()
