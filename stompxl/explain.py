"""Plain-English explanations of a block's settings, for learning the pedal.

One Claude call per block: what the model is, what it's doing in this preset,
and for each setting what it does, how to set it, and what this song wants.
"""

from __future__ import annotations

import json
from typing import Any

from stompxl.helixgen.llm import LLMCaller, LLMGenerationError, LLMRequest

from . import engine


def _schema() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "role", "parameters", "tips"],
        "properties": {
            "summary": text,
            "role": text,
            "parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "what", "how", "recommended", "why"],
                    "properties": {"name": text, "what": text, "how": text,
                                   "recommended": text, "why": text},
                },
            },
            "tips": {"type": "array", "items": text},
        },
    }


def prompt(block: dict[str, Any], snapshots: list[dict[str, Any]], context: dict[str, Any]) -> str:
    """``block`` and ``snapshots`` are as the version view shows them."""
    model = engine.catalog().get(block["model"])
    lines = []
    for p in block["params"]:
        extra = []
        edit = p.get("edit") or {}
        if edit.get("kind") == "choice":
            extra.append("options: " + ", ".join(edit.get("options", [])))
        elif edit.get("min") is not None:
            extra.append(f"range {edit['min']:g} to {edit['max']:g}")
        if p.get("controlled_by") and p["controlled_by"] != "snapshot":
            extra.append(f"moved by {p['controlled_by']}")
        per_snap = [f"{s['name']}: {v['value']}" for s in snapshots
                    for v in (s.get("values") or {}).get(block["key"], []) if v["name"] == p["name"]]
        if per_snap:
            extra.append("per snapshot " + ", ".join(per_snap))
        lines.append(f"- {p['name']}: {p['value']}" + (f" ({'; '.join(extra)})" if extra else ""))
    on_in = [s["name"] for s in snapshots if s.get("enabled", {}).get(block["key"], True)]
    return "\n".join([
        "Explain one block of an HX Stomp XL preset to a guitarist who wants to learn the pedal.",
        "",
        f"Song: {context.get('song', '?')}",
        f"Guitar: {context.get('guitar', '?')}",
        f"Output rig: {context.get('rig', '?')}",
        f"Block: {model.display_name} ({model.category or 'block'}"
        + (f", based on {model.based_on}" if model.based_on else "") + ")",
        f"Why it's in the preset: {block.get('why') or 'not recorded'}",
        f"On in snapshots: {', '.join(on_in) or 'none'}"
        + (f"; on footswitch FS{block['footswitch']}" if block.get("footswitch") else ""),
        "",
        "Its settings, with the values in this preset (in the units HX Edit shows):",
        *lines,
        "",
        "Answer in plain English, no jargon without a short explanation:",
        "- summary: what this model is (the real gear it's based on, its character) in 2-3 sentences.",
        "- role: what it's doing in THIS preset and song, in 1-2 sentences.",
        "- parameters: one entry per setting above, using exactly the names given. 'what': what "
        "it does to the sound, concretely (e.g. what you'd hear turning it up). 'how': how to set "
        "it in general, typical ranges and what extremes do. 'recommended': a value or range for "
        "this song and guitar (say if the current value is already right). 'why': the reason, "
        "tied to the song, guitar or rig.",
        "- tips: 2-4 practical tips for this block on the Stomp XL (gain staging, interactions "
        "with neighbouring blocks, live use).",
    ])


def explain(block: dict[str, Any], snapshots: list[dict[str, Any]], context: dict[str, Any],
            call: LLMCaller) -> dict[str, Any]:
    request = LLMRequest(prompt=prompt(block, snapshots, context), schema=_schema(),
                         schema_name="explain")
    text = call(request)
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(f"Explanation wasn't valid JSON: {exc}") from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("parameters"), list):
        raise LLMGenerationError("Explanation had no parameters")
    return {"model": block["model"], **answer}
