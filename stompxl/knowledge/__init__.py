"""The best-practices library every prompt is built on.

Each ``*.md`` file in this directory is one topic. They are read in name order
and sent whole, so adding a file (or editing one) changes every generation
without touching code.

The files speak of the pedal through ``{{PEDAL}}``, ``{{SNAPSHOT_COUNT}}``,
``{{SNAPSHOT_SWITCHING}}`` and ``{{SWITCH_NOTES}}``, filled in for the pedal in
use (HX Stomp XL or HX Stomp).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


def topics() -> list[Path]:
    return sorted(_DIR.glob("*.md"))


def library_text() -> str:
    from stompxl import xl

    return _library_text(xl.pedal().key)


@lru_cache(maxsize=4)
def _library_text(pedal_key: str) -> str:
    from stompxl import xl

    pedal = xl.pedal(pedal_key)
    text = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in topics())
    for token, value in {
        "{{PEDAL}}": pedal.name,
        "{{SNAPSHOT_COUNT}}": str(pedal.snapshot_count),
        "{{SNAPSHOT_SWITCHING}}": pedal.snapshot_switching,
        "{{SWITCH_NOTES}}": pedal.switch_notes,
    }.items():
        text = text.replace(token, value)
    return text
