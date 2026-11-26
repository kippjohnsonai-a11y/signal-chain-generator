"""Quick picks: one-tap moves in the pedal close-up, like "Less fizz".

A pick belongs to every block of a kind (``category``, e.g. Amp) or to one
model (``model``, e.g. Cali Rectifire). Its changes are written the way they
read, in the units the editor shows::

    Presence -1, Treble -0.5      nudge
    LowCut = 100                  set
    Decay x0.75                   scale
    Gain|Drive = 1                whichever of the two the block has

The library holds them, so they're edited in the app and every device sees the
same set. ``quick_picks.json`` is only the starting set and the close-up's main
controls per kind.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

DEFAULTS = Path(__file__).with_name("quick_picks.json")
KINDS = ("category", "model")
LABEL_LIMIT = 40
MAX_CHANGES = 12

_PARAM = r"[A-Za-z][\w /]*?(?:\|[A-Za-z][\w /]*?)*"
_CHANGE = re.compile(
    rf"^\s*(?P<param>{_PARAM})\s*(?:"
    r"(?P<set>=)\s*(?P<setv>.+?)"
    r"|(?P<mul>[x\u00d7*])\s*(?P<mulv>[0-9.]+)"
    r"|(?P<addv>[+-]\s*[0-9.]+)"
    r")\s*$")


class PickError(ValueError):
    pass


@cache
def _defaults() -> dict[str, Any]:
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def main_controls() -> dict[str, list[str]]:
    """The close-up's main controls per kind of block, most-used first."""
    return _defaults()["main"]


def default_picks(category: str | None = None) -> list[dict[str, Any]]:
    """The starting picks, as rows for the library."""
    rows = []
    for name, picks in _defaults()["picks"].items():
        if category is not None and name != category:
            continue
        for position, pick in enumerate(picks):
            label, changes, *match = pick
            rows.append({"kind": "category", "name": name, "label": label,
                         "changes": parse(changes), "match": match[0] if match else "",
                         "position": position})
    return rows


def _number(text: str) -> float:
    try:
        return float(text.replace(" ", ""))
    except ValueError as exc:
        raise PickError(f"{text!r} isn't a number") from exc


def parse(text: str) -> list[dict[str, Any]]:
    """``"Presence -1, LowCut = 100"`` as ``[{param, op, value}, ...]``."""
    changes = []
    for part in [p for p in str(text).split(",") if p.strip()]:
        found = _CHANGE.match(part)
        if not found:
            raise PickError(f"Couldn't read {part.strip()!r}: write it like 'Presence -1', "
                            "'LowCut = 100' or 'Decay x0.75'")
        param = "|".join(p.strip() for p in found["param"].split("|"))
        if found["set"]:
            raw = found["setv"].strip()
            try:
                value: Any = float(raw)
            except ValueError:
                value = raw
            changes.append({"param": param, "op": "set", "value": value})
        elif found["mul"]:
            changes.append({"param": param, "op": "mul", "value": _number(found["mulv"])})
        else:
            changes.append({"param": param, "op": "add", "value": _number(found["addv"])})
    if not changes:
        raise PickError("A quick pick needs at least one change")
    if len(changes) > MAX_CHANGES:
        raise PickError(f"A quick pick can change at most {MAX_CHANGES} settings")
    return changes


def _num(value: float) -> str:
    return f"{value:g}"


def format_changes(changes: list[dict[str, Any]]) -> str:
    """The text :func:`parse` reads back to the same changes."""
    out = []
    for c in changes:
        value = c["value"]
        if c["op"] == "set":
            out.append(f"{c['param']} = {_num(value) if isinstance(value, (int, float)) else value}")
        elif c["op"] == "mul":
            out.append(f"{c['param']} x{_num(value)}")
        else:
            out.append(f"{c['param']} {'+' if value >= 0 else '-'}{_num(abs(value))}")
    return ", ".join(out)


def clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Validated fields for the library, from what the app sent."""
    out: dict[str, Any] = {}
    if "kind" in fields:
        if fields["kind"] not in KINDS:
            raise PickError("A quick pick is for a kind of block or one model")
        out["kind"] = fields["kind"]
    if "name" in fields:
        name = " ".join(str(fields["name"] or "").split())
        if not name:
            raise PickError("Say which kind of block or model the pick is for")
        out["name"] = name[:80]
    if "label" in fields:
        label = " ".join(str(fields["label"] or "").split())
        if not label:
            raise PickError("Give the quick pick a name")
        out["label"] = label[:LABEL_LIMIT]
    if "changes" in fields:
        raw = fields["changes"]
        out["changes"] = parse(raw) if isinstance(raw, str) else parse(format_changes(raw))
    if "match" in fields:
        out["match"] = " ".join(str(fields["match"] or "").split())[:40]
    return out
