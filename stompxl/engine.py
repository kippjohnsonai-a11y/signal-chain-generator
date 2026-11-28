"""Song + guitar + rig in, an HX Stomp XL (or HX Stomp) preset out.

Generation runs four structured Claude rounds, each constrained to a JSON
schema built from the model catalog:

0. **Brief** - what the song needs: sections (one per snapshot), the gear on
   the recording, how to adapt it to this guitar, where wah / expression /
   looper belong.
1. **Blocks** - the signal chain, checked against the rig and the toggles.
2. **Parameters** - every block's settings, in one call so gain is staged
   across the chain.
3. **Snapshots** - four snapshots, one per section.

A refinement ("too muddy") runs rounds 1-3 again with the current preset's
values and the feedback in the prompt, so the model changes what the feedback
asks for and leaves the rest alone.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from stompxl.helixgen import llm as hg
from stompxl.helixgen import resources
from stompxl.helixgen.dataset import CONTINUOUS, ModelCatalog, ModelDefinition
from stompxl.helixgen.generator import SNAPSHOT_CONTROLLER, generate_preset
from stompxl.helixgen.llm import LLMCaller, LLMGenerationError, LLMRequest
from stompxl.helixgen.validator import PresetValidator

from . import config, knowledge, xl

Progress = Callable[[str], None]

RIG_RULES = {
    "full": (
        "Rig: headphones / FRFR / PA. The chain MUST include exactly one amp (category Amp) "
        "and one cab (category Cab) right after it."
    ),
    "front": (
        "Rig: into the front of a real guitar amp. The chain must NOT include any amp, "
        "preamp or cab block; the real amp supplies the core tone."
    ),
    "4cm": (
        "Rig: 4-cable method. The chain must NOT include amp or cab blocks. It MUST include "
        f"one '{xl.FX_LOOP_MODEL}' block standing in for the amp's preamp: drives, wah and "
        "dynamics before it; modulation, delay and reverb after it."
    ),
    "nam_pedal": (
        "Rig: NAM on the pedal (NAM A2 Lite firmware) into headphones / FRFR / PA. A Neural "
        "Amp Modeler capture is the amp: pick it in 'nam_capture' from the player's list. The "
        "app inserts the NAM block itself, so the chain must NOT include any amp or preamp "
        f"block, nor '{xl.NAM_MODEL}'. List the pre-amp blocks (drives, wah, dynamics, Poly "
        "Capo) first, then the post-amp ones. Mark every block's 'amp_side': 'before' or "
        "'after' the NAM amp; the app puts the NAM block exactly between the last 'before' "
        "and the first 'after' block, so an EQ, filter or pitch block goes wherever you mark "
        "it (an EQ that shapes the amp's tone, e.g. a mud cut, is 'after'). If the capture is "
        "amp only, include exactly one cab (category Cab), 'after'; if it includes its cab, "
        "no cab. Modulation, delay and reverb come after."
    ),
    "nam_usb": (
        "Rig: NAM on the computer over USB. A Neural Amp Modeler capture and a cab IR run on "
        "the computer between the pedal's pre-amp and post-amp effects. The chain must NOT "
        f"include any amp, preamp or cab block, nor '{xl.RETURN_MODEL}' (the app adds it). "
        "List the pre-amp blocks (noise gate, compressor, wah, drives, Poly Capo) first, then "
        "the post-amp ones (modulation, delay, reverb, volume, looper): everything from the "
        "first post-amp block on runs after NAM. Mark every block's 'amp_side': 'before' or "
        "'after' NAM; the chain splits between the last 'before' and the first 'after' block "
        "(an EQ shaping the amp's tone is 'after'). Put the amp you want in 'nam_capture': one "
        "from the player's list, or describe the capture to load if the list has none that "
        "fits. The Input block's gate sits after NAM here, so gate in a Noise Gate block "
        "before the drives if the song needs one."
    ),
}

#: Where a block sits relative to the NAM amp, as Claude marks it.
AMP_SIDES = ("before", "after")

#: Blocks the app adds for a rig, which Claude never chooses.
MANAGED_MODELS = (xl.NAM_MODEL, xl.RETURN_MODEL)


@lru_cache(maxsize=1)
def catalog() -> ModelCatalog:
    return ModelCatalog(resources.dataset_path())


@dataclass
class Setup:
    """Everything a generation is conditioned on besides the song itself."""

    guitar: dict[str, Any]
    rig: dict[str, Any]
    options: dict[str, bool]
    preferences: list[str] = field(default_factory=list)
    instructions: str = ""
    exp_pedal: str = xl.DEFAULT_EXP_PEDAL
    gate_mode: str = xl.DEFAULT_GATE
    #: The player's NAM captures this rig can play (xl.captures_for).
    captures: list[dict[str, Any]] = field(default_factory=list)
    #: The player's gear notes: ``{"kind": "nam" | "model" | "category", "name", "text"}``,
    #: default behaviour whenever that capture, model or kind of block is used.
    gear_notes: list[dict[str, Any]] = field(default_factory=list)

    def _notes(self, kind: str) -> dict[str, list[str]]:
        """Note texts of one kind, by the gear's name as the player wrote it."""
        out: dict[str, list[str]] = {}
        for note in self.gear_notes:
            if note.get("kind") == kind and str(note.get("text") or "").strip():
                out.setdefault(str(note["name"]).strip(), []).append(note["text"].strip())
        return out

    def gear_lines(self) -> list[str]:
        """The model and category notes, as prompt lines."""
        return ([f"- Whenever the chain uses '{name}': {text}"
                 for name, texts in self._notes("model").items() for text in texts]
                + [f"- Whenever the chain uses any {name} block: {text}"
                   for name, texts in self._notes("category").items() for text in texts])

    @property
    def nam(self) -> str | None:
        """"pedal" or "usb" for a NAM rig, else None."""
        kind = self.rig["kind"]
        return kind[4:] if kind in xl.NAM_RIGS else None

    def describe(self) -> str:
        g = self.guitar
        lines = [
            f"Guitar: {g['name']} - pickups: {g['pickups']}"
            + (f" ({g['pickup_notes']})" if g.get("pickup_notes") else "")
            + f"; tuning: {g.get('tuning') or 'E standard'}"
            + (f"; notes: {g['notes']}" if g.get("notes") else ""),
            RIG_RULES[self.rig["kind"]] + (f" Player's note: {self.rig['notes']}" if self.rig.get("notes") else ""),
            "Toggles: "
            + ", ".join(
                f"{name} {'ON' if self.options.get(name) else 'OFF'}"
                for name in ("wah", "expression", "looper", "capo")
            ),
            "Expression pedal: " + {
                "single": "ONE ordinary expression pedal on EXP 1, no toe switch. It can sweep only "
                          "one block per preset: a wah, OR a Volume pedal, OR a Pitch Wham.",
                "mission": "Line 6 Mission pedal with toe switch: EXP 1 sweeps the wah or Pitch Wham, "
                           "EXP 2 the Volume pedal; the toe switch flips between them.",
                "dual": "two expression pedals: EXP 1 for wah or Pitch Wham, EXP 2 for Volume.",
            }.get(self.exp_pedal, ""),
            "Input noise gate: " + {
                "on": "ON for every preset; set its threshold for this song, guitar and gain.",
                "auto": "your call per song (on for crunch and high gain, off for clean or ambient).",
                "off": "OFF.",
            }.get(self.gate_mode, ""),
        ]
        if self.nam and self.options.get("full_rig"):
            lines.append(
                "Full-rig captures only: the player has no DSP to spare for a cab, so the capture "
                "must be one that includes its cab (the list below has only those)"
                + (", and the chain has no cab block." if self.nam == "pedal" else ".")
                + ("" if self.captures else
                   " If you describe a capture to load, make it a full rig (amp and cab)."))
        if self.nam:
            nam_notes = {k.lower(): v for k, v in self._notes("nam").items()}
            lines.append(
                "The player's NAM captures (a 'player's note' is how they want that capture "
                "handled whenever you pick it):\n" + "\n".join(
                    f"- {xl.describe_capture(c)}"
                    + "".join(f" Player's note: {t}" for t in nam_notes.get(c["name"].lower(), []))
                    for c in self.captures)
                if self.captures else
                "The player has listed no NAM captures yet: describe the capture to load "
                "(amp, channel, gain, with or without cab) in 'nam_capture'."
            )
        if self.options.get("capo"):
            lines.append(
                f"Poly Capo ON: the chain includes one '{xl.CAPO_MODEL}' block (polyphonic pitch "
                "shift, ~40% of the DSP), early in the chain before drives and the amp, on in every "
                "snapshot. Set its Interval to the semitones that take the player's guitar and "
                "tuning to the recording's pitch (e.g. +2 for a song played with a capo at fret 2, "
                "-1 for an Eb-tuned song on a guitar in E standard); Mix 100%. Keep the rest of the "
                "chain inside the DSP room below."
            )
            lines.append(xl.describe_room(xl.dsp_room(self.rig["kind"], self.options,
                                                      config.read_settings())))
        if self.preferences:
            lines.append(
                "The player's standing instructions (always honour these):\n"
                + "\n".join(f"- {p}" for p in self.preferences)
            )
        if gear := self.gear_lines():
            lines.append(
                "The player's gear notes: default handling for a model or kind of block. Only "
                "for gear the chain actually uses; follow the note (set the values, or add the "
                "EQ or other block it asks for when the block and DSP limits allow, otherwise "
                "say so in 'why'):\n" + "\n".join(gear))
        if self.instructions.strip():
            lines.append(f"Extra instructions for this preset: {self.instructions.strip()}")
        return "\n".join(lines)


@dataclass
class Result:
    brief: dict[str, Any]
    chain: dict[str, Any]
    preset: dict[str, Any]
    notes: str
    warnings: list[str]


# ---------------------------------------------------------------------------
# Round 0: the song brief


def _brief_schema() -> dict[str, Any]:
    string = {"type": "string"}
    section = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "<= 10 characters, e.g. INTRO"},
            "role": string,
            "description": string,
            "pickup": string,
            "guitar_knobs": string,
        },
        "required": ["name", "role", "description", "pickup", "guitar_knobs"],
        "additionalProperties": False,
    }
    toggle = {
        "type": "object",
        "properties": {"recommended": {"type": "boolean"}, "how": string},
        "required": ["recommended", "how"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": string,
            "artist": string,
            "preset_name": {"type": "string", "description": "<= 16 characters"},
            "tuning": string,
            "tempo_bpm": {"type": "number"},
            "reference_gear": string,
            "tone_summary": string,
            "guitar_adaptation": string,
            "sections": {"type": "array", "items": section},
            "wah": toggle,
            "expression": toggle,
            "looper": toggle,
            "capo": toggle,
            "playing_tips": {"type": "array", "items": string},
        },
        "required": [
            "title", "artist", "preset_name", "tuning", "tempo_bpm", "reference_gear",
            "tone_summary", "guitar_adaptation", "sections", "wah", "expression", "looper",
            "capo", "playing_tips",
        ],
        "additionalProperties": False,
    }


def _parse_brief(text: str) -> dict[str, Any]:
    brief = hg._parse_json_response(text)
    if not isinstance(brief, dict):
        raise LLMGenerationError("The brief must be a JSON object.")
    sections = brief.get("sections")
    if not isinstance(sections, list) or len(sections) != xl.SNAPSHOT_COUNT:
        raise LLMGenerationError(
            f"The brief must list exactly {xl.SNAPSHOT_COUNT} sections, one per snapshot."
        )
    for section in sections:
        section["name"] = str(section.get("name", "")).strip()[: xl.SNAPSHOT_NAME_LIMIT].upper()
    brief["preset_name"] = str(brief.get("preset_name") or brief.get("title") or "Song")[
        : xl.PRESET_NAME_LIMIT
    ]
    try:
        brief["tempo_bpm"] = max(40.0, min(240.0, float(brief.get("tempo_bpm") or 120)))
    except (TypeError, ValueError):
        brief["tempo_bpm"] = 120.0
    return brief


def _brief_prompt(song: str, setup: Setup) -> str:
    return "\n\n".join([
        f"Plan a Line 6 {xl.PEDAL_NAME} preset for a guitarist who wants to play this song. "
        "Work out the guitar tone(s) on the original recording and how to get them with "
        "the player's own guitar and rig.",
        "Reference - best practices:\n" + knowledge.library_text(),
        "Answer rules:\n"
        f"- Exactly {xl.SNAPSHOT_COUNT} sections, in the order they occur or are most useful live; each becomes a "
        f"snapshot. Names at most {xl.SNAPSHOT_NAME_LIMIT} characters, upper case.\n"
        f"- preset_name at most {xl.PRESET_NAME_LIMIT} characters.\n"
        "- For wah, expression and looper say whether the song benefits from it and exactly "
        "how/where to use it, even if the player's toggle is OFF (the recommendation is shown "
        "to them).\n"
        "- capo: whether a Poly Capo block (pitch-shifts the whole guitar, no retuning) helps "
        "this song with this guitar's tuning, and by how many semitones (e.g. the recording "
        "uses a capo, or is tuned down a half step).\n"
        "- guitar_adaptation: what you will change because of this guitar and rig versus the recording.\n"
        "- If you are not sure of the song, say so in tone_summary and design for the genre.",
        setup.describe(),
        f"Song: {song.strip()}",
    ])


# ---------------------------------------------------------------------------
# Round 1: block selection


def _blocks_schema(cat: ModelCatalog, setup: Setup | None = None) -> dict[str, Any]:
    names = sorted({m.display_name for m in cat.models()}, key=str.lower)
    schema = {
        "type": "object",
        "properties": {
            "interpretation": {"type": "string"},
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string", "enum": names},
                        "why": {"type": "string"},
                    },
                    "required": ["model", "why"],
                    "additionalProperties": False,
                },
            },
            "input_gate": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "threshold_db": {"type": "number"},
                    "why": {"type": "string"},
                },
                "required": ["enabled", "threshold_db", "why"],
                "additionalProperties": False,
            },
        },
        "required": ["interpretation", "blocks", "input_gate"],
        "additionalProperties": False,
    }
    if setup is not None and setup.nam:
        capture: dict[str, Any] = {"type": "string",
                                   "description": "The NAM capture that plays the amp"}
        if setup.nam == "pedal" or setup.captures:
            names = [c["name"] for c in setup.captures]
            if setup.nam == "usb":
                capture["description"] += (": one of the player's, or a description of "
                                           "another capture to load")
            else:
                capture["enum"] = names
        schema["properties"]["nam_capture"] = capture
        schema["required"].append("nam_capture")
        item = schema["properties"]["blocks"]["items"]
        item["properties"]["amp_side"] = {
            "type": "string", "enum": list(AMP_SIDES),
            "description": "Whether the block sits before or after the NAM amp in the chain"}
        item["required"].append("amp_side")
    return schema


def check_blocks(models: list[ModelDefinition], setup: Setup,
                 sides: list[str | None] | None = None) -> None:
    """Raise LLMGenerationError naming the first rule the chain breaks.

    ``sides`` are Claude's 'amp_side' marks on a NAM rig, block by block.
    """

    if not models:
        raise LLMGenerationError("The chain needs at least one block.")
    room = xl.MAX_BLOCKS - (1 if setup.nam else 0)
    if len(models) > room:
        raise LLMGenerationError(
            f"The {xl.PEDAL_NAME} holds at most {xl.MAX_BLOCKS} blocks"
            + (" and the app adds one for NAM, so choose at most "
               f"{room}." if setup.nam else "."))
    total = xl.dsp_total(models) + (xl.NAM_DSP if setup.nam == "pedal" else 0.0)
    budget = xl.dsp_budget(config.read_settings())
    if total > budget:
        heaviest = sorted(models, key=xl.dsp_cost, reverse=True)[:3]
        raise LLMGenerationError(
            f"Too heavy for the {xl.PEDAL_NAME}'s single DSP (about {total:.0f}% estimated, limit "
            f"{budget:.0f}%). Heaviest: "
            + ", ".join(f"{m.display_name} ~{xl.dsp_cost(m):.0f}%" for m in heaviest)
            + (
                f". Keep the {xl.CAPO_MODEL} (the player turned it on) and drop or swap a "
                "cheaper block: "
                + xl.describe_room(xl.dsp_room(setup.rig["kind"], setup.options,
                                               config.read_settings()))
                if setup.options.get("capo") else
                ". Drop or swap one of them (polyphonic pitch blocks are the most expensive; "
                "for a down-tuned song prefer asking the player to retune)."
            )
        )
    cats = [(m.category or "").lower() for m in models]
    names = [m.display_name for m in models]
    kind = setup.rig["kind"]
    amps = cats.count("amp")
    cabs = cats.count("cab")
    if kind == "full":
        if amps != 1 or cabs != 1:
            raise LLMGenerationError("A full-rig preset needs exactly one amp and one cab.")
        if cats.index("cab") != cats.index("amp") + 1:
            raise LLMGenerationError("The cab must come directly after the amp.")
    elif setup.nam:
        managed = [n for n in names if n in MANAGED_MODELS]
        if managed:
            raise LLMGenerationError(
                f"Leave out '{managed[0]}': the app adds the NAM routing itself.")
        if amps or any("Preamp" in n for n in names):
            raise LLMGenerationError("NAM is the amp here: remove every amp and preamp block.")
        if setup.nam == "usb" and cabs:
            raise LLMGenerationError(
                "The cab IR runs on the computer with NAM: remove the cab block.")
        if setup.nam == "pedal" and cabs > 1:
            raise LLMGenerationError("Use at most one cab block.")
        if sides is not None and all(side in AMP_SIDES for side in sides):
            slot = sides.index("after") if "after" in sides else len(sides)
            if "before" in sides[slot:]:
                stray = models[slot + sides[slot:].index("before")].display_name
                raise LLMGenerationError(
                    f"'{stray}' is marked 'before' the amp but comes after a block marked "
                    "'after': list every 'before' block first, then every 'after' block.")
            early = [m.display_name for m in models[:slot]
                     if xl.is_post_amp(m)]
            if early:
                raise LLMGenerationError(
                    f"'{early[0]}' belongs after the amp: mark it 'after' and list it after "
                    "the drives.")
        else:
            slot = xl.amp_slot(models)
        late = [m.display_name for m in models[slot:]
                if (m.category or "") in xl.PRE_AMP_CATEGORIES
                or m.display_name == xl.CAPO_MODEL]
        if late:
            raise LLMGenerationError(
                f"'{late[0]}' must come before the amp: list every drive, wah and dynamics "
                "block before the cab and the modulation, delay and reverb blocks.")
    else:
        if amps or cabs:
            raise LLMGenerationError(
                "This rig uses a real amp: remove every amp and cab block."
            )
    if kind == "4cm" and names.count(xl.FX_LOOP_MODEL) != 1:
        raise LLMGenerationError(f"The 4-cable method needs exactly one '{xl.FX_LOOP_MODEL}' block.")

    wahs = cats.count(xl.WAH_CATEGORY.lower())
    if setup.options.get("wah") and wahs != 1:
        raise LLMGenerationError("The wah toggle is ON: include exactly one wah block.")
    if not setup.options.get("wah") and wahs:
        raise LLMGenerationError("The wah toggle is OFF: remove the wah block.")

    loopers = names.count(xl.LOOPER_MODEL)
    if setup.options.get("looper"):
        if loopers != 1 or names[-1] != xl.LOOPER_MODEL:
            raise LLMGenerationError(
                f"The looper toggle is ON: '{xl.LOOPER_MODEL}' must be the last block, once."
            )
    elif loopers:
        raise LLMGenerationError("The looper toggle is OFF: remove the looper block.")

    capos = names.count(xl.CAPO_MODEL)
    if setup.options.get("capo") and capos != 1:
        raise LLMGenerationError(f"The Poly Capo toggle is ON: include exactly one '{xl.CAPO_MODEL}'.")
    if not setup.options.get("capo") and capos:
        raise LLMGenerationError(f"The Poly Capo toggle is OFF: remove the '{xl.CAPO_MODEL}' block.")

    swept = [m for m in models if xl.default_expression(m) is not None]
    if setup.options.get("expression") and not swept:
        raise LLMGenerationError(
            "The expression toggle is ON: include a block the pedal can sweep "
            "(a wah, 'Volume' or 'Pitch Wham')."
        )
    if len(swept) > 1 and setup.exp_pedal == "single":
        raise LLMGenerationError(
            "The player has ONE expression pedal: keep only one of the wah / Volume / Pitch "
            "Wham blocks."
        )
    if not setup.options.get("expression") and not setup.options.get("wah") and swept:
        raise LLMGenerationError(
            "The expression and wah toggles are OFF: remove the Volume / Pitch Wham block."
        )


def _parse_blocks(text: str, cat: ModelCatalog, setup: Setup
                  ) -> tuple[list[dict[str, Any]], str, dict[str, Any], dict[str, Any] | None]:
    answer = hg._parse_json_response(text)
    if not isinstance(answer, dict) or not isinstance(answer.get("blocks"), list):
        raise LLMGenerationError("The answer must be an object with a 'blocks' list.")
    blocks: list[dict[str, Any]] = []
    models: list[ModelDefinition] = []
    for entry in answer["blocks"]:
        name = entry.get("model") if isinstance(entry, dict) else entry
        try:
            model = cat.get(str(name))
        except Exception as exc:
            raise LLMGenerationError(f"Unknown model '{name}'.") from exc
        models.append(model)
        block = {"model": model.display_name, "why": str(entry.get("why", ""))}
        side = entry.get("amp_side") if isinstance(entry, dict) else None
        if setup.nam and side in AMP_SIDES:
            block["amp_side"] = side
        blocks.append(block)
    sides = [b.get("amp_side") for b in blocks] if setup.nam else None
    check_blocks(models, setup, sides)
    capture = _parse_capture(answer.get("nam_capture"), models, setup)
    return (blocks, str(answer.get("interpretation", "")),
            _parse_gate(answer.get("input_gate"), setup), capture)


def _parse_capture(raw: Any, models: list[ModelDefinition], setup: Setup
                   ) -> dict[str, Any] | None:
    """The capture a NAM rig plays, checked against the cab rule."""
    if not setup.nam:
        return None
    wanted = str(raw or "").strip()
    capture = next((c for c in setup.captures if c["name"].lower() == wanted.lower()), None)
    if capture is None:
        if setup.nam == "pedal":
            raise LLMGenerationError(
                "nam_capture must be one of: " + ", ".join(c["name"] for c in setup.captures))
        if not wanted:
            raise LLMGenerationError("Name or describe the NAM capture in 'nam_capture'.")
        capture = {"name": wanted[:80], "slot": None, "description": "",
                   "cab": bool(setup.options.get("full_rig")),
                   "suggested": True}
    if setup.nam == "pedal":
        cabs = sum(1 for m in models if (m.category or "") == "Cab")
        if capture["cab"] and cabs:
            raise LLMGenerationError(
                f"The capture '{capture['name']}' includes its cab: remove the cab block.")
        if not capture["cab"] and cabs != 1:
            raise LLMGenerationError(
                f"The capture '{capture['name']}' is amp only: include exactly one cab block.")
    return dict(capture)


def _parse_gate(raw: Any, setup: Setup) -> dict[str, Any]:
    """The Input block's gate, clamped and held to the player's gate setting."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        threshold = float(raw.get("threshold_db", -55))
    except (TypeError, ValueError):
        threshold = -55.0
    low, high = xl.GATE_THRESHOLD_RANGE
    enabled = bool(raw.get("enabled"))
    if setup.nam == "usb":
        # The Input block is NAM's return here: a gate on it would gate the amp.
        enabled = False
    elif setup.gate_mode == "on":
        enabled = True
    elif setup.gate_mode == "off":
        enabled = False
    return {
        "enabled": enabled,
        "threshold_db": round(max(low, min(high, threshold)), 1),
        "why": str(raw.get("why", "")),
    }


def _blocks_prompt(song: str, setup: Setup, brief: dict[str, Any], cat: ModelCatalog,
                   current: dict[str, Any] | None, feedback: str) -> str:
    parts = [
        f"Choose the signal chain (blocks, in order) for this {xl.PEDAL_NAME} preset.",
        "Reference - best practices:\n" + knowledge.library_text(),
        "Rules:\n"
        f"- At most {xl.MAX_BLOCKS} blocks, named exactly as in the model list.\n"
        f"- {RIG_RULES[setup.rig['kind']]}\n"
        "- Wah toggle ON: exactly one wah block. OFF: none.\n"
        f"- Looper toggle ON: '{xl.LOOPER_MODEL}' as the last block. OFF: none.\n"
        f"- Poly Capo toggle ON: exactly one '{xl.CAPO_MODEL}', early (before drives and the "
        "amp). OFF: none.\n"
        "- Expression toggle ON: include at least one block the pedal sweeps (wah, 'Volume' "
        "or 'Pitch Wham'). With both expression and wah OFF, no Volume / Pitch Wham block.\n"
        "- Every block must earn its place across the 4 snapshots; respect the DSP budget.\n"
        "- Expression ON with the wah OFF: use the pedal as a 'Volume' block (after the amp and "
        "cab, before delay and reverb) unless the song clearly needs a Pitch Wham.\n"
        "- input_gate: the Input block's built-in noise gate (costs no block). Follow the "
        "player's gate setting and the gate guidance in the reference; give the threshold in dB.\n"
        "- 'why' says in one short sentence what each block does for this song.",
        setup.describe(),
        f"Song: {song}\nBrief:\n{json.dumps(brief, indent=1)}",
    ]
    if current is not None:
        parts.append(
            "Current chain:\n" + "\n".join(_current_lines(current, cat))
            + (f"\nCurrent NAM capture: {current['nam']['capture']['name']}"
               if (current.get("nam") or {}).get("capture") else "")
            + "\nCurrent input gate: " + json.dumps(current.get("gate") or current.get("input") or {})
            + f"\n\nThe player's feedback on it: {feedback.strip()}\n"
            "Keep the current blocks unless the feedback is best solved by adding, removing or "
            "swapping one. In 'interpretation', say in 1-3 sentences what the feedback means in "
            "tone terms and what you will change."
        )
    else:
        parts.append("In 'interpretation', summarise the approach in 1-2 sentences.")
    parts.append("Available models by category (name - real-world reference):\n"
                 + hg._summarize_catalog(cat))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Round 2: parameters


def _current_lines(current: dict[str, Any], cat: ModelCatalog) -> list[str]:
    """The current chain for the prompt, marking where the NAM amp sits."""
    blocks = current["blocks"]
    mode = (current.get("nam") or {}).get("mode")
    if mode == "pedal":
        split = next((i for i, b in enumerate(blocks) if b.get("nam")), len(blocks))
    elif mode == "usb":
        split = xl.nam_split(blocks, [cat.get(b["model"]) for b in blocks])
    else:
        split = None
    lines: list[str] = []
    number = 0
    for index, block in enumerate(blocks):
        if index == split:
            lines.append("   [NAM amp]")
        if block["model"] in MANAGED_MODELS:
            continue
        number += 1
        side = "" if split is None else (" (before the amp)" if index < split
                                         else " (after the amp)")
        lines.append(f"{number}. {block['model']}{side}")
    if split is not None and split >= len(blocks):
        lines.append("   [NAM amp]")
    return lines


def _render_with_current(key: str, model: ModelDefinition, summary: list[dict[str, Any]],
                         current: dict[str, Any] | None) -> str:
    text = hg._render_block(key, model, summary)
    if current is None:
        return text
    now = {
        entry["name"]: hg._current_value(model.get_parameter(entry["name"]), current.get(entry["name"]))
        for entry in summary
    }
    return text + "\n  current values: " + json.dumps(now)


def _parameters_round(call: LLMCaller, song: str, setup: Setup, brief: dict[str, Any],
                      blocks: list[dict[str, Any]], models: list[ModelDefinition],
                      previous: list[dict[str, Any] | None], feedback: str,
                      report: Progress) -> None:
    targets: dict[str, tuple[ModelDefinition, list[dict[str, Any]]]] = {}
    for index, model in enumerate(models):
        summary = [e for e in hg._summarize_parameters(model) if e["name"] != xl.PEDAL_PARAMETER
                   or xl.default_expression(model) is None]
        if summary:
            targets[f"block{index + 1}"] = (model, summary)
    if not targets:
        return

    rendered = "\n\n".join(
        _render_with_current(
            key, model, summary,
            (previous[int(key[5:]) - 1] or {}).get("parameters") if previous[int(key[5:]) - 1] else None,
        )
        for key, (model, summary) in targets.items()
    )
    guidance = (
        f"The player's feedback on the current settings: {feedback.strip()}\n"
        "Start from the current values and change only what the feedback calls for (plus "
        "whatever a swapped-in block needs). Blocks without current values are new."
        if feedback else
        "Set values that recreate the song's tone on this guitar and rig."
    )
    prompt = "\n\n".join([
        f"Set the parameters of every block in this {xl.PEDAL_NAME} preset.",
        "Reference - best practices:\n" + knowledge.library_text(),
        "Rules:\n"
        "- Stay within each parameter's range; for options use a label exactly as listed.\n"
        "- Stage gain across the whole chain and tailor the values to the player's guitar "
        "(see the guitar section of the reference).\n"
        "- These are the base values; snapshots are designed in a later step, so set the "
        "values for the song's main sound (usually the main riff / rhythm part).\n"
        "- Use null to keep a parameter's default.\n"
        '- Answer {"blocks": {"block1": {"<parameter>": value, ...}, ...}} for every block listed.',
        setup.describe(),
        f"Song: {song}\nBrief: {brief.get('tone_summary', '')}\n"
        f"Adaptation: {brief.get('guitar_adaptation', '')}",
        guidance,
        "Chain: " + " -> ".join(m.display_name for m in models),
        "Blocks to configure:\n\n" + rendered,
    ])
    report(f"Setting parameters for {len(targets)} block(s)...")
    chosen = hg._ask(
        call,
        LLMRequest(prompt=prompt,
                   schema=hg._parameters_schema({k: s for k, (_, s) in targets.items()}),
                   schema_name="parameters"),
        lambda text: hg._parse_parameters_response(text, {k: m for k, (m, _) in targets.items()}),
        on_retry=lambda: report("Retrying parameters..."),
    )
    for key, values in chosen.items():
        blocks[int(key[5:]) - 1]["parameters"] = values


# ---------------------------------------------------------------------------
# Round 3: snapshots


def _snapshots_round(call: LLMCaller, song: str, setup: Setup, brief: dict[str, Any],
                     blocks: list[dict[str, Any]], models: list[ModelDefinition],
                     previous_snapshots: list[dict[str, Any]] | None, feedback: str,
                     report: Progress) -> list[dict[str, Any]]:
    targets: dict[str, tuple[ModelDefinition, list[dict[str, Any]]]] = {}
    for index, (block, model) in enumerate(zip(blocks, models, strict=True)):
        summary: list[dict[str, Any]] = []
        chosen = block.get("parameters", {})
        for entry in hg._summarize_parameters(model):
            definition = model.get_parameter(entry["name"])
            if (definition.controller_range() is None or entry["name"] == xl.PEDAL_PARAMETER
                    or xl.is_stereo_only(entry["name"])):
                continue
            entry["current"] = hg._current_value(definition, chosen.get(entry["name"]))
            summary.append(entry)
        targets[f"block{index + 1}"] = (model, summary)

    sections = "\n".join(
        f"{i + 1}. {s['name']}: {s['role']} - {s['description']}"
        for i, s in enumerate(brief.get("sections", []))
    )
    parts = [
        f"Design the {xl.SNAPSHOT_COUNT} snapshots of this {xl.PEDAL_NAME} preset, one per song "
        "section, in this order:\n" + sections,
        "Reference - snapshot and gain-staging practice:\n" + knowledge.library_text(),
        "Rules:\n"
        f"- Exactly {xl.SNAPSHOT_COUNT} snapshots, named as the sections above (<= "
        f"{xl.SNAPSHOT_NAME_LIMIT} characters).\n"
        "- Every block starts ON at the value shown as 'now'. Use null for anything a "
        "snapshot leaves alone; write only what it changes.\n"
        "- Snapshots switch blocks on and off; they share one set of values. Leave every "
        "parameter null unless a section cannot work without it (a clean intro in a heavy "
        "song that must drop the amp's Drive a long way, say), or the player asks for it. "
        "Never nudge a value (a Mid, a Level, a Mix) a little between snapshots: the player "
        "dials each block once, by hand, for the whole song. Lift a solo by switching a "
        "drive or delay on, not by raising levels.\n"
        "- Keep amp and cab blocks on in every snapshot.\n"
        "- A wah block is on only in the snapshots that use wah. Looper and Poly Capo blocks "
        "stay on in every snapshot.\n"
        '- Answer {"snapshots": [{"name": "INTRO", "blocks": {"block1": {"enabled": '
        'false, "parameters": {...}}, ...}}, ...]}',
        setup.describe(),
        f"Song: {song}\nBrief: {brief.get('tone_summary', '')}",
    ]
    if previous_snapshots is not None:
        parts.append(
            "Current snapshot design (block keys match the list below):\n"
            + json.dumps(previous_snapshots, indent=1)
            + f"\nThe player's feedback: {feedback.strip()}\nKeep the design except where the "
            "feedback calls for a change (feedback about one section changes that snapshot only)."
        )
    parts.append("Blocks, in signal order:\n\n" + "\n\n".join(
        hg._render_snapshot_block(k, m, s) for k, (m, s) in targets.items()
    ))
    report(f"Designing {xl.SNAPSHOT_COUNT} snapshots...")
    snapshots = hg._ask(
        call,
        LLMRequest(prompt="\n\n".join(parts),
                   schema=hg._snapshots_schema({k: s for k, (_, s) in targets.items()},
                                               count=xl.SNAPSHOT_COUNT),
                   schema_name="snapshots"),
        lambda text: hg._parse_snapshots_response(
            text, {k: m for k, (m, _) in targets.items()}, count=xl.SNAPSHOT_COUNT
        ),
        on_retry=lambda: report("Retrying snapshots..."),
    )
    return settle_snapshots(snapshots, blocks, models, feedback)


#: A snapshot may move a knob away from the block's own value only by at least
#: this share of the knob's range; anything smaller is a nudge the player would
#: have to chase by hand in every snapshot, so it is dropped.
SNAPSHOT_MIN_MOVE = 0.25


def settle_snapshots(snapshots: list[dict[str, Any]], blocks: list[dict[str, Any]],
                     models: list[ModelDefinition], feedback: str = ""
                     ) -> list[dict[str, Any]]:
    """Snapshots that switch blocks on and off, sharing one set of values.

    Small per-snapshot moves of a continuous knob are dropped, so each block
    has one value to dial by hand. Big, deliberate moves (an amp's Drive cut
    for a clean intro) stay, as do switches and option changes. A snapshot the
    player's feedback names (e.g. "the solo needs more mids") is left as
    Claude wrote it: there the player asked for the change.
    """
    asked = feedback.lower()
    out = []
    for snap in snapshots:
        name = str(snap.get("name") or "").strip().lower()
        if name and re.search(rf"\b{re.escape(name)}\b", asked):
            out.append(snap)
            continue
        settled: dict[str, Any] = {}
        for key, change in (snap.get("blocks") or {}).items():
            index = int(key)
            if not 0 <= index < len(models):
                settled[key] = change
                continue
            model, base = models[index], blocks[index].get("parameters") or {}
            kept = {parameter: value
                    for parameter, value in (change.get("parameters") or {}).items()
                    if not _small_move(model, parameter, base.get(parameter), value)}
            entry = {k: v for k, v in change.items() if k != "parameters"}
            if kept:
                entry["parameters"] = kept
            if entry:
                settled[key] = entry
        out.append({**snap, "blocks": settled})
    return out


def _small_move(model: ModelDefinition, parameter: str, base: Any, value: Any) -> bool:
    if parameter not in model.parameters:
        return False
    definition = model.get_parameter(parameter)
    span = definition.controller_range()
    if (definition.value_type != CONTINUOUS or span is None or isinstance(value, bool)
            or not isinstance(value, (int, float))):
        return False
    low, high = span
    if high <= low:
        return False
    if base is None:
        base = definition.default_value()
    if not isinstance(base, (int, float)) or isinstance(base, bool):
        return False
    return abs(float(value) - float(base)) < SNAPSHOT_MIN_MOVE * (high - low)


# ---------------------------------------------------------------------------
# Assembly


def _remap_snapshots(snapshots: list[dict[str, Any]], mapping: dict[int, int]
                     ) -> list[dict[str, Any]]:
    """Re-key each snapshot's block changes after blocks move; dropped blocks vanish."""
    out = []
    for snap in snapshots:
        blocks = {
            str(mapping[int(k)]): v
            for k, v in (snap.get("blocks") or {}).items()
            if int(k) in mapping
        }
        out.append({**snap, "blocks": blocks})
    return out


def _finish_chain(blocks: list[dict[str, Any]], models: list[ModelDefinition],
                  snapshots: list[dict[str, Any]], cat: ModelCatalog,
                  exp_pedal: str = xl.DEFAULT_EXP_PEDAL, stage_boost: bool = False,
                  ) -> tuple[list[dict[str, Any]], list[ModelDefinition], list[dict[str, Any]]]:
    """Add the volume boost (and the Stage boost), then wire expression pedals
    and footswitches.

    The boost is the app's, not Claude's: any Gain block the chain carries is
    replaced by a fresh one, off in every snapshot, when a block slot is free.
    """
    keep = [i for i, m in enumerate(models) if m.display_name != xl.BOOST_MODEL]
    mapping = {old: new for new, old in enumerate(keep)}
    blocks = [blocks[i] for i in keep]
    models = [models[i] for i in keep]
    snapshots = _remap_snapshots(snapshots, mapping)

    if len(blocks) < xl.MAX_BLOCKS:
        # Post-amp, ahead of the time effects, so it lifts the whole tone and
        # the delay and reverb tails with it; the looper stays last.
        amp = max((i for i, m in enumerate(models)
                   if (m.category or "") in ("Amp", "Cab") or blocks[i].get("nam")),
                  default=-1)
        at = next((i for i, m in enumerate(models)
                   if i > amp and ((m.category or "") in ("Delay", "Reverb")
                                   or m.display_name == xl.LOOPER_MODEL)), len(models))
        boost = cat.get(xl.BOOST_MODEL)
        blocks.insert(at, {
            "model": boost.display_name,
            "why": f"Volume boost (+{xl.BOOST_DB:g} dB) for solos, on its own footswitch.",
            "parameters": {"Gain": xl.BOOST_DB},
            "enabled": False,
        })
        models.insert(at, boost)
        snapshots = _remap_snapshots(
            snapshots, {i: (i if i < at else i + 1) for i in range(len(models))})

    if stage_boost:
        blocks, models, snapshots = _add_stage_boost(blocks, models, snapshots, cat)

    # Blocks whose bypass the snapshots must not touch: the boost is a stomp
    # you step on, and the looper keeps playing across snapshot changes.
    fixed = {i for i, m in enumerate(models)
             if m.display_name in (xl.BOOST_MODEL, xl.LOOPER_MODEL)
             or blocks[i].get("stage_boost")}
    for index in fixed:
        # Snapshot Bypass off: changing snapshot leaves a stomped boost or a
        # playing looper as it is, instead of recalling a stored on/off.
        blocks[index]["no_snapshot_bypass"] = True
    for snap in snapshots:
        for index in fixed:
            change = snap["blocks"].get(str(index))
            if change is not None:
                change.pop("enabled", None)
                if not change:
                    del snap["blocks"][str(index)]

    swept = [i for i, m in enumerate(models) if xl.default_expression(m) is not None]
    used: set[int] = set()
    for i in swept:
        want = xl.default_expression(models[i])
        if exp_pedal == "single" or len(swept) == 1:
            pedal = 1
        else:
            pedal = want if want not in used else (2 if want == 1 else 1)
        blocks[i]["expression"] = pedal
        used.add(pedal)

    assign_switches(blocks, models)
    return blocks, models, snapshots


def assign_switches(blocks: list[dict[str, Any]], models: list[ModelDefinition]) -> None:
    """Put the chain's stomps on the current pedal's footswitches, in place."""
    switches = xl.switch_map(config.read_settings())
    for block in blocks:
        for key in ("footswitch", "footswitch_label", "switch"):
            block.pop(key, None)
    stage = {i for i, b in enumerate(blocks) if b.get("stage_boost")}
    for index, switch in xl.plan_footswitches(models, skip=stage).items():
        blocks[index]["footswitch"] = switches[switch]
        blocks[index]["footswitch_label"] = xl.switch_label(models[index])
        blocks[index]["switch"] = switch


def for_current_pedal(chain: dict[str, Any], preset: dict[str, Any],
                      cat: ModelCatalog | None = None) -> tuple[dict[str, Any], list[str]]:
    """The preset to send to the pedal in use: as saved, or rebuilt for it.

    A version made for the other pedal (an XL song on an HX Stomp, or the other
    way) gets its footswitches planned again for this pedal's switches and its
    snapshots fitted to this pedal's count before it is built again.
    """
    if (preset.get("data") or {}).get("device") == xl.DEVICE_ID:
        return preset, []
    cat = cat or catalog()
    chain = copy.deepcopy(chain)
    assign_switches(chain.get("blocks") or [], [cat.get(b["model"]) for b in chain.get("blocks") or []])
    rebuilt, warnings = build_preset(chain, cat)
    return rebuilt, [f"Rebuilt for the {xl.PEDAL_NAME}.", *warnings]


STAGE_BOOST_WHY = ("Stage boost: more gain into the amp, off until you switch it on in "
                   "Stage mode (Gain boost).")


def stage_boost_at(blocks: list[dict[str, Any]], models: list[ModelDefinition]) -> int | None:
    """Where the Stage boost goes: just before the amp (or NAM block), else None."""
    return next((i for i, m in enumerate(models)
                 if (m.category or "") == "Amp" or blocks[i].get("nam")), None)


def _add_stage_boost(blocks: list[dict[str, Any]], models: list[ModelDefinition],
                     snapshots: list[dict[str, Any]], cat: ModelCatalog,
                     ) -> tuple[list[dict[str, Any]], list[ModelDefinition], list[dict[str, Any]]]:
    """An off drive just before the amp, when a block slot and the DSP allow.

    A drive the chain already has there of the same model becomes the Stage
    boost instead of getting a twin (a re-dial keeps the one it had)."""
    at = stage_boost_at(blocks, models)
    if at is None:
        return blocks, models, snapshots
    if at and models[at - 1].display_name == xl.STAGE_BOOST_MODEL:
        blocks[at - 1]["stage_boost"] = True
        return blocks, models, snapshots
    boost = cat.get(xl.STAGE_BOOST_MODEL)
    budget = xl.dsp_budget(config.read_settings())
    if len(blocks) >= xl.MAX_BLOCKS or xl.dsp_total([*models, boost]) > budget:
        return blocks, models, snapshots
    blocks.insert(at, {"model": boost.display_name, "why": STAGE_BOOST_WHY, "parameters": {},
                       "enabled": False, "stage_boost": True})
    models.insert(at, boost)
    snapshots = _remap_snapshots(
        snapshots, {i: (i if i < at else i + 1) for i in range(len(models) - 1)})
    return blocks, models, snapshots


def build_preset(chain: dict[str, Any], cat: ModelCatalog | None = None
                 ) -> tuple[dict[str, Any], list[str]]:
    """Assemble and validate the .hlx document for a chain."""

    cat = cat or catalog()
    chain = copy.deepcopy(chain)
    count = xl.SNAPSHOT_COUNT
    dropped = (chain.get("snapshots") or [])[count:]
    if dropped:
        # A song made for the XL's four snapshots, built for the Stomp's three.
        chain["snapshots"] = chain["snapshots"][:count]
    if (chain.get("nam") or {}).get("mode") == "usb":
        # Re-derived on every build, so a hand edit's added or removed block
        # lands on the right path.
        route_usb(chain["blocks"], [cat.get(b["model"]) for b in chain["blocks"]])
    preset, report = generate_preset(chain, cat, xl.template())
    # HelixGen gives every block without a switch one of FS1-3; only the
    # planned switches belong in the preset.
    planned = {f"block{i}" for i, b in enumerate(chain["blocks"]) if "footswitch" in b}
    fs = preset["data"]["tone"].get("footswitch", {}).get("dsp0", {})
    for key in [k for k in fs if k not in planned]:
        del fs[key]
    warnings = list(report.warnings)
    for n, snap in enumerate(dropped, start=count + 1):
        warnings.append(f"Snapshot {n} ({snap.get('name') or 'unnamed'}) left off: "
                        f"the {xl.PEDAL_NAME} has {count} snapshots.")
    # Every snapshot plays at the song's tempo unless it carries its own (an
    # imported preset can); the template's are all 120, and with Tempo set to
    # per-snapshot the delays would follow those.
    tone = preset["data"]["tone"]
    specs = chain.get("snapshots") or []
    for index in range(xl.SNAPSHOT_COUNT):
        own = specs[index].get("tempo") if index < len(specs) else None
        tone[f"snapshot{index}"]["@tempo"] = (
            float(own) if isinstance(own, (int, float)) else tone["global"]["@tempo"])
    # HelixGen writes the HX Stomp's Snapshots source; the XL uses its own.
    # (On the Stomp the two are the same.)
    for params in preset["data"]["tone"].get("controller", {}).get("dsp0", {}).values():
        for entry in params.values():
            if entry.get("@controller") == SNAPSHOT_CONTROLLER:
                entry["@controller"] = xl.CONTROLLER_SNAPSHOT
    # The path mix, where the chain carries one (an imported preset does).
    join = chain.get("join")
    if isinstance(join, dict):
        tone["dsp0"]["join"].update({k: v for k, v in join.items() if k in xl.JOIN_PARAMS})
    if (chain.get("nam") or {}).get("mode") == "usb":
        warnings += _apply_usb_routing(tone)
    assigned = xl.assign_expression(preset, chain, cat)
    validator = PresetValidator(resources.schema_path(), cat)
    errors = validator.validate_structural(preset) + validator.validate_semantic(preset)
    if errors:
        raise LLMGenerationError("Generated preset failed validation: " + "; ".join(map(str, errors)))
    return preset, warnings + [f"Assigned {a}" for a in assigned]


def _apply_usb_routing(tone: dict[str, Any]) -> list[str]:
    """Split at the start, path B out of the Send, path A in from USB 5/6."""
    dsp0 = tone["dsp0"]
    dsp0["split"].update({"@position": 0, "BalanceA": 0.5, "BalanceB": 0.5})
    dsp0["join"]["@position"] = xl.MAX_BLOCKS
    routing = xl.usb_routing(config.read_settings())
    if routing is None:
        return ["NAM over USB: in HX Edit set the Input block to USB 5/6 and path B's Output "
                "to Send L/R (run 'stompxl learn-nam-usb' once and the app does this itself)."]
    dsp0["inputA"]["@input"] = routing["input"]
    dsp0["outputB"]["@output"] = routing["send_output"]
    return []


def _brief_notes(brief: dict[str, Any], interpretation: str, blocks: list[dict[str, Any]],
                 gate: dict[str, Any] | None = None) -> str:
    lines = []
    if interpretation:
        lines.append(interpretation)
    if gate:
        state = f"on at {gate['threshold_db']:g} dB" if gate["enabled"] else "off"
        lines.append(f"- Input noise gate: {state}" + (f" ({gate['why']})" if gate.get("why") else ""))
    lines.extend(f"- {b['model']}: {b['why']}" for b in blocks if b.get("why"))
    return "\n".join(lines)


def generate(song: str, setup: Setup, call: LLMCaller, report: Progress = lambda _m: None,
             cat: ModelCatalog | None = None) -> Result:
    cat = cat or catalog()
    report("Researching the song...")
    brief = hg._ask(
        call,
        LLMRequest(prompt=_brief_prompt(song, setup), schema=_brief_schema(), schema_name="brief"),
        _parse_brief,
        on_retry=lambda: report("Retrying the brief..."),
    )
    report(f"Brief ready: {brief['title']} - {brief['artist']}")
    return _build(song, setup, call, report, cat, brief, current=None, feedback="")


def brief_only(song: str, setup: Setup, call: LLMCaller, report: Progress = lambda _m: None
               ) -> dict[str, Any]:
    """Just the song plan (round 1), for a preset built by hand or imported."""
    return hg._ask(
        call,
        LLMRequest(prompt=_brief_prompt(song, setup), schema=_brief_schema(), schema_name="brief"),
        _parse_brief,
        on_retry=lambda: report("Retrying the plan..."),
    )


def refine(song: str, setup: Setup, call: LLMCaller, current: dict[str, Any],
           brief: dict[str, Any], feedback: str, report: Progress = lambda _m: None,
           cat: ModelCatalog | None = None) -> Result:
    """A new version of ``current`` that answers ``feedback``.

    Also how a fork to another guitar or rig works: the change is described as
    feedback and the preset re-dialled from where it stands.
    """
    return _build(song, setup, call, report, cat or catalog(), brief, current=current,
                  feedback=feedback)


#: Blocks the app sets itself; a re-dial leaves their values alone.
_FIXED_VALUE_MODELS = (*MANAGED_MODELS, xl.BOOST_MODEL, xl.LOOPER_MODEL)


def retune(song: str, setup: Setup, call: LLMCaller, current: dict[str, Any],
           brief: dict[str, Any], feedback: str, report: Progress = lambda _m: None,
           cat: ModelCatalog | None = None) -> Result:
    """``current`` re-dialled for ``setup`` (usually another guitar), same signal chain.

    Unlike :func:`refine`, Claude does not choose blocks: every block stays where
    it is, with its model, footswitch, expression pedal, NAM capture and input
    gate. Only the parameter and snapshot rounds run again, starting from the
    current values, so the settings follow the new guitar.
    """
    cat = cat or catalog()
    chain = copy.deepcopy(current)
    blocks = chain["blocks"]
    models = [cat.get(b["model"]) for b in blocks]
    # The blocks Claude re-dials, by their index in the chain. The NAM block,
    # Return, boost and looper carry the app's own values.
    dialled = [i for i, m in enumerate(models) if m.display_name not in _FIXED_VALUE_MODELS]
    if not dialled:
        raise LLMGenerationError("This chain has no blocks to re-dial.")
    sub_blocks = [blocks[i] for i in dialled]
    sub_models = [models[i] for i in dialled]
    old_params = [dict(b.get("parameters") or {}) for b in sub_blocks]
    # The adaptation was written for the old guitar; the feedback says what changed.
    brief_now = {**brief, "guitar_adaptation": feedback.strip()}

    report("Keeping the chain: " + " -> ".join(b["model"] for b in blocks))
    _parameters_round(call, song, setup, brief_now, sub_blocks, sub_models,
                      [{"parameters": p} for p in old_params], feedback, report)
    for block, model, before in zip(sub_blocks, sub_models, old_params, strict=True):
        # Parameters the round doesn't offer (an expression-swept pedal position,
        # say) keep their value; the ones it offers are what Claude answered.
        offered = {e["name"] for e in hg._summarize_parameters(model)
                   if e["name"] != xl.PEDAL_PARAMETER or xl.default_expression(model) is None}
        block["parameters"] = {**{k: v for k, v in before.items() if k not in offered},
                               **(block.get("parameters") or {})}

    # Snapshots: Claude sees the re-dialled blocks only (as the first build
    # did, before the app added its own); the app's blocks keep their changes.
    sub_index = {chain_i: sub_i for sub_i, chain_i in enumerate(dialled)}
    previous_snapshots = [
        {"name": snap.get("name"),
         "blocks": {f"block{sub_index[int(k)] + 1}": v
                    for k, v in (snap.get("blocks") or {}).items() if int(k) in sub_index}}
        for snap in current.get("snapshots") or []
    ]
    sub_snapshots = _snapshots_round(call, song, setup, brief_now, sub_blocks, sub_models,
                                     previous_snapshots, feedback, report)
    snapshots = []
    old_snaps = current.get("snapshots") or []
    for n, snap in enumerate(sub_snapshots):
        old = old_snaps[n] if n < len(old_snaps) else {}
        kept = {k: v for k, v in (old.get("blocks") or {}).items() if int(k) not in sub_index}
        moved = {str(dialled[int(k)]): v for k, v in (snap.get("blocks") or {}).items()}
        snapshots.append({**old, **snap, "blocks": {**kept, **moved}})
    chain["snapshots"] = snapshots

    report("Assembling and validating the preset...")
    preset, warnings = build_preset(chain, cat)
    report("Done.")
    notes = _brief_notes(brief_now, "Same signal chain, re-dialled: " + feedback.strip(),
                         blocks, chain.get("gate"))
    if chain.get("nam"):
        notes = nam_notes(chain["nam"]) + "\n" + notes
    return Result(brief=brief_now, chain=chain, preset=preset, notes=notes, warnings=warnings)


def _build(song: str, setup: Setup, call: LLMCaller, report: Progress, cat: ModelCatalog,
           brief: dict[str, Any], current: dict[str, Any] | None, feedback: str) -> Result:
    if setup.nam == "pedal" and not setup.captures:
        raise LLMGenerationError(
            "NAM on the pedal needs your captures: in Settings > NAM captures, tap \"Which NAMs "
            "are on my pedal?\" (one tap if you have the starter pack), or add them with their IR slots.")
    report("Choosing blocks...")
    blocks, interpretation, gate, capture = hg._ask(
        call,
        LLMRequest(prompt=_blocks_prompt(song, setup, brief, cat, current, feedback),
                   schema=_blocks_schema(cat, setup), schema_name="chain"),
        lambda text: _parse_blocks(text, cat, setup),
        on_retry=lambda: report("Retrying block selection..."),
    )
    models = [cat.get(b["model"]) for b in blocks]
    report("Chain: " + " -> ".join(b["model"] for b in blocks))

    # Carry each kept block's settings across, matching by model in order, so a
    # refinement starts from where the preset stands rather than from defaults.
    previous: list[dict[str, Any] | None] = [None] * len(blocks)
    index_map: dict[int, int] = {}
    if current is not None:
        pool = list(enumerate(current["blocks"]))
        for i, block in enumerate(blocks):
            for j, (old_index, old) in enumerate(pool):
                if old["model"] == block["model"]:
                    previous[i] = old
                    index_map[old_index] = i
                    pool.pop(j)
                    break

    _parameters_round(call, song, setup, brief, blocks, models, previous, feedback, report)

    previous_snapshots = None
    if current is not None:
        previous_snapshots = []
        for snap in current.get("snapshots") or []:
            moved = {
                f"block{index_map[int(k)] + 1}": v
                for k, v in (snap.get("blocks") or {}).items()
                if int(k) in index_map
            }
            previous_snapshots.append({"name": snap.get("name"), "blocks": moved})

    snapshots = _snapshots_round(call, song, setup, brief, blocks, models, previous_snapshots,
                                 feedback, report)
    if setup.nam:
        blocks, models, snapshots = _add_nam_blocks(blocks, models, snapshots, cat, setup.nam,
                                                    capture)
    blocks, models, snapshots = _finish_chain(blocks, models, snapshots, cat, setup.exp_pedal,
                                              bool(setup.options.get("stage_boost")))

    chain = {
        "title": brief["preset_name"],
        "meta": {"name": brief["preset_name"]},
        "global": {"@tempo": float(brief.get("tempo_bpm") or 120)},
        "input": {
            "noiseGate": gate["enabled"],
            "threshold": gate["threshold_db"],
            "decay": xl.GATE_DECAY_DEFAULT,
        },
        "gate": gate,
        "blocks": blocks,
        "snapshots": snapshots,
    }
    if setup.nam:
        chain["nam"] = {"mode": setup.nam, "capture": capture}
    report("Assembling and validating the preset...")
    preset, warnings = build_preset(chain, cat)
    report("Done.")
    notes = _brief_notes(brief, interpretation, blocks, gate)
    if setup.nam:
        notes = nam_notes(chain["nam"]) + "\n" + notes
    return Result(brief=brief, chain=chain, preset=preset, notes=notes, warnings=warnings)


def nam_notes(nam: dict[str, Any]) -> str:
    capture = nam.get("capture") or {}
    if nam.get("mode") == "pedal":
        return (f"NAM capture: {capture.get('name')} from IR slot {capture.get('slot')} "
                f"(the {xl.NAM_TITLE} block shows as '{xl.NAM_MODEL}' in HX Edit).")
    load = ("Load this capture" if not capture.get("suggested")
            else "No capture in your list fits; find one like this")
    return (f"NAM on the computer: {load}: {capture.get('name')}"
            + ("" if capture.get("cab") else ", plus a cab IR") + ".")


def _add_nam_blocks(blocks: list[dict[str, Any]], models: list[ModelDefinition],
                    snapshots: list[dict[str, Any]], cat: ModelCatalog, mode: str,
                    capture: dict[str, Any] | None,
                    ) -> tuple[list[dict[str, Any]], list[ModelDefinition], list[dict[str, Any]]]:
    """Insert the block a NAM rig needs: the NAM amp, or the guitar's Return.

    On the pedal the NAM block takes the amp's place (between the blocks
    Claude marked before and after it, see xl.nam_split) with the capture's IR slot on its Model knob. On the
    computer a Return block heads path B, bringing the guitar in from the
    Return L jack (route_usb puts the blocks on their paths).
    """
    if mode == "pedal":
        # Between Claude's last 'before' and first 'after' block, so the chain
        # plays in the order its notes describe (an EQ marked after stays after).
        at = xl.nam_split(blocks, models)
        model = cat.get(xl.NAM_MODEL)
        block = {
            "model": model.display_name,
            "why": f"{xl.NAM_TITLE}: the '{(capture or {}).get('name')}' capture is the amp.",
            "parameters": {**xl.NAM_DEFAULTS,
                           xl.NAM_SLOT_PARAMETER: float((capture or {}).get("slot") or 1)},
            "nam": True,
        }
    else:
        at = 0
        model = cat.get(xl.RETURN_MODEL)
        block = {
            "model": model.display_name,
            "why": "Brings the guitar in from the Return L jack; path B sends it to NAM.",
            "parameters": {"Mix": 1.0, "Return": 0.0},
            "no_snapshot_bypass": True,
        }
    blocks = [*blocks[:at], block, *blocks[at:]]
    models = [*models[:at], model, *models[at:]]
    snapshots = _remap_snapshots(
        snapshots, {i: (i if i < at else i + 1) for i in range(len(models) - 1)})
    return blocks, models, snapshots


def route_usb(blocks: list[dict[str, Any]], models: list[ModelDefinition]) -> None:
    """Put a NAM-on-computer chain on its two paths, in place.

    Path B (1) is the Return block and the pre-amp blocks, ending at the Send
    output to the computer; path A (0) is everything from the first post-amp
    block on, after NAM comes back on the Input block. Positions count along
    each path from the split at the start.
    """
    slot = xl.nam_split(blocks, models)
    positions = [0, 0]
    for index, block in enumerate(blocks):
        path = 1 if index < slot else 0
        block["path"] = path
        block["position"] = positions[path]
        positions[path] += 1


def diff_chains(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable changes between two chains, for the version history."""

    out: list[str] = []
    old_models = [b["model"] for b in old.get("blocks", [])]
    new_models = [b["model"] for b in new.get("blocks", [])]
    if old_models != new_models:
        out.append("Chain: " + " -> ".join(old_models) + "  =>  " + " -> ".join(new_models))
    pool = list(old.get("blocks", []))
    for block in new.get("blocks", []):
        match = next((b for b in pool if b["model"] == block["model"]), None)
        if match is None:
            continue
        pool.remove(match)
        before, after = match.get("parameters", {}), block.get("parameters", {})
        out.extend(
            f"{block['model']} {name}: {_fmt(before.get(name))} -> {_fmt(after.get(name))}"
            for name in sorted(set(before) | set(after))
            if before.get(name) != after.get(name)
        )
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "default"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
