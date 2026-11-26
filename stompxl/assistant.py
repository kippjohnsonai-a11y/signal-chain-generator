"""Ask Claude about the preset on the pedal: answers plus changes to tap.

One Claude call per question. Claude sees the chain as the editor shows it
(display units, ranges, options), the player's My tone tweaks and gear notes,
and the conversation so far. It answers in words and may suggest a few small
sets of changes; each change is checked against the chain here, so the app
only offers what it can play on the pedal. Nothing is applied until the
player taps a suggestion.
"""

from __future__ import annotations

import json
import re
from typing import Any

from stompxl.helixgen.llm import LLMCaller, LLMGenerationError, LLMRequest

from . import knowledge

#: The pseudo-parameter for a block's on/off.
ENABLED = "@enabled"
MAX_TURNS = 12
MAX_SUGGESTIONS = 4


def _schema() -> dict[str, Any]:
    text = {"type": "string"}
    change = {
        "type": "object", "additionalProperties": False,
        "required": ["block", "parameter", "value"],
        "properties": {"block": {"type": "integer"}, "parameter": text, "value": text},
    }
    suggestion = {
        "type": "object", "additionalProperties": False,
        "required": ["label", "why", "changes"],
        "properties": {"label": text, "why": text,
                       "changes": {"type": "array", "items": change}},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["reply", "suggestions"],
        "properties": {"reply": text, "suggestions": {"type": "array", "items": suggestion}},
    }


def _param_line(p: dict[str, Any], snap_value: str | None) -> str:
    edit = p.get("edit") or {}
    extra = []
    if edit.get("kind") == "choice":
        extra.append("options: " + ", ".join(edit.get("options", [])))
    elif edit.get("kind") == "toggle":
        extra.append("On/Off")
    elif edit.get("min") is not None:
        extra.append(f"range {edit['min']:g} to {edit['max']:g}")
    if p.get("controlled_by") and p["controlled_by"] != "snapshot":
        extra.append(f"moved by {p['controlled_by']}")
    value = snap_value if snap_value is not None else p["value"]
    if snap_value is not None:
        extra.append("this snapshot's own value")
    return (f"    - {p['param']} (\"{p['name']}\"): {value}"
            + (f" [{'; '.join(extra)}]" if extra else ""))


def describe_view(view: dict[str, Any], snapshot: int) -> str:
    """The chain as prompt lines, values as the pedal plays them in ``snapshot``."""
    snaps = view.get("snapshots") or []
    snap = snaps[snapshot] if 0 <= snapshot < len(snaps) else {"enabled": {}, "values": {}}
    lines = [f"Tempo {round(view.get('tempo') or 120)} BPM. Snapshots: "
             + (", ".join(f"{i + 1} {s['name']}" for i, s in enumerate(snaps)) or "none")
             + (f". The pedal is on snapshot {snapshot + 1} ({snap.get('name')})." if snaps else ".")]
    for blk in view.get("blocks", []):
        on = blk["enabled"] if blk.get("stomp") else snap.get("enabled", {}).get(blk["key"], True)
        own = {v["param"]: v["value"] for v in (snap.get("values") or {}).get(blk["key"], [])}
        head = (f"Block {blk['index']}: {blk['model']} ({blk.get('category') or 'block'}"
                + (f", based on {blk['based_on']}" if blk.get("based_on") else "") + ")"
                + f", {'ON' if on else 'OFF'}"
                + (f", footswitch FS{blk['footswitch']}" if blk.get("footswitch") else ""))
        lines.append(head)
        lines += [_param_line(p, own.get(p["param"])) for p in blk.get("params", [])]
    for blk in view.get("io") or []:
        lines.append(f"{blk['title']} (not editable from suggestions): "
                     + ", ".join(f"{p['name']} {p['value']}" for p in blk["params"]))
    return "\n".join(lines)


def prompt(view: dict[str, Any], snapshot: int, messages: list[dict[str, str]],
           context: dict[str, Any]) -> str:
    prefs = context.get("preferences") or []
    gear = context.get("gear_notes") or []
    guitar = context.get("guitar")
    turns = [m for m in messages if str(m.get("text") or "").strip()][-MAX_TURNS:]
    talk = "\n".join(f"{'Player' if m.get('role') == 'user' else 'You'}: {m['text'].strip()}"
                     for m in turns)
    return "\n".join([
        "You're beside a guitarist who is tweaking the preset loaded on their HX Stomp XL right "
        "now, by ear. Answer their latest message about it.",
        "",
        f"Preset: \"{context.get('name') or '?'}\" in slot {context.get('slot') or '?'}.",
        f"Guitar: {guitar}" if guitar else "Guitar: not said.",
        *(["Their standing tone tweaks (My tone; honour these):", *[f"- {p}" for p in prefs]]
          if prefs else []),
        *(["Their gear notes:", *[f"- {g}" for g in gear]] if gear else []),
        "",
        "The chain, in the units HX Edit shows (parameter id first, then its label):",
        describe_view(view, snapshot),
        "",
        "Reference - best practices:",
        knowledge.library_text(),
        "",
        "Conversation so far (latest last):",
        talk or "(nothing yet)",
        "",
        "Answer in 'reply': plain, short, practical (a few sentences, or a short list). Explain "
        "the why in terms of what they'll hear.",
        f"If changes would help, give up to {MAX_SUGGESTIONS} 'suggestions' they can tap to apply, "
        "each a small, coherent move (label: 2-4 words like 'Less fizz'; why: one sentence). Each "
        "change names the block number, the parameter id exactly as listed, and the new value as "
        "text in display units (a number inside the range, or one of the options, or On/Off). To "
        f"switch a block on or off use parameter '{ENABLED}' with value 'on' or 'off'. Only "
        "values of blocks that are already in the chain: no new blocks, swaps or moves (say so "
        "in the reply if one would help, and they can add it with Edit chain). Changes play in "
        "the snapshot the pedal is on. No suggestions when the question doesn't call for any.",
    ])


def _number(text: str) -> float | None:
    found = re.search(r"-?\d+(?:\.\d+)?", str(text))
    return float(found.group()) if found else None


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def check_change(view: dict[str, Any], change: dict[str, Any]) -> dict[str, Any] | None:
    """A suggested change the app can apply, or None if it names nothing real."""
    blocks = view.get("blocks", [])
    index = change.get("block")
    if not isinstance(index, int) or not 0 <= index < len(blocks):
        return None
    blk = blocks[index]
    param = str(change.get("parameter") or "").strip()
    raw = str(change.get("value") or "").strip()
    if param == ENABLED or _norm(param) in ("enabled", "onoff", "bypass"):
        on = _norm(raw) in ("on", "true", "1", "enabled", "yes")
        if not on and _norm(raw) not in ("off", "false", "0", "disabled", "no", "bypass"):
            return None
        return {"block": index, "enabled": on, "label": f"{blk['model']} {'on' if on else 'off'}"}
    found = next((p for p in blk["params"] if p["param"] == param), None) or next(
        (p for p in blk["params"] if _norm(p["param"]) == _norm(param) or _norm(p["name"]) == _norm(param)),
        None)
    if found is None:
        return None
    edit = found.get("edit") or {}
    kind = edit.get("kind")
    if kind == "number":
        value = _number(raw)
        if value is None:
            return None
        if edit.get("min") is not None:
            value = min(edit["max"], max(edit["min"], value))
        shown = f"{value:g}"
    elif kind == "toggle":
        if _norm(raw) not in ("on", "off", "true", "false"):
            return None
        shown = "On" if _norm(raw) in ("on", "true") else "Off"
    elif kind == "choice":
        shown = next((o for o in edit.get("options", []) if _norm(o) == _norm(raw)), None)
        if shown is None:
            return None
    else:
        return None
    return {"block": index, "param": found["param"], "name": found["name"], "value": shown,
            "label": f"{blk['model']} {found['name']} {shown}"}


def ask(view: dict[str, Any], snapshot: int, messages: list[dict[str, str]],
        context: dict[str, Any], call: LLMCaller) -> dict[str, Any]:
    request = LLMRequest(prompt=prompt(view, snapshot, messages, context), schema=_schema(),
                         schema_name="pedal_chat")
    text = call(request)
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(f"Claude's answer wasn't valid JSON: {exc}") from exc
    if not isinstance(answer, dict):
        raise LLMGenerationError("Claude's answer had no reply")
    suggestions = []
    dropped = 0
    for s in (answer.get("suggestions") or [])[:MAX_SUGGESTIONS]:
        if not isinstance(s, dict):
            continue
        changes = [c for c in (check_change(view, x) for x in s.get("changes") or []
                               if isinstance(x, dict)) if c]
        dropped += len(s.get("changes") or []) - len(changes)
        if changes:
            suggestions.append({"label": str(s.get("label") or "Suggestion")[:40],
                                "why": str(s.get("why") or ""), "changes": changes})
    return {"reply": str(answer.get("reply") or "").strip(), "suggestions": suggestions,
            "dropped": dropped}
