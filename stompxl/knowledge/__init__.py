"""The best-practices library every prompt is built on.

Each ``*.md`` file in this directory is one topic. They are read in name order
and sent whole, so adding a file (or editing one) changes every generation
without touching code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


def topics() -> list[Path]:
    return sorted(_DIR.glob("*.md"))


@lru_cache(maxsize=1)
def library_text() -> str:
    return "\n\n".join(path.read_text().strip() for path in topics())
