"""What Claude costs: a rough dollar figure for each job and for the month.

Each call's token counts go to ``claude-costs.jsonl`` in the data folder of
whoever made it. The prices are Anthropic's list prices per million tokens; a
model this table doesn't know is priced like the newest Opus, so a figure is
never left out. They are estimates: the Anthropic console has the real bill.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from . import config

logger = logging.getLogger(__name__)

#: (input, output) dollars per million tokens, by model id prefix. Longest match wins.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-5": (0.10, 0.50),
    "claude-haiku-4": (1.0, 5.0),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]
#: Cache writes cost a quarter more than input; cache reads a tenth of it.
CACHE_WRITE = 1.25
CACHE_READ = 0.1

_FILE = "claude-costs.jsonl"
_lock = threading.Lock()
_meter: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("claude_meter", default=None)


def price(model: str) -> tuple[float, float]:
    match = max((p for p in PRICES if model.startswith(p)), key=len, default=None)
    return PRICES[match] if match else DEFAULT_PRICE


def _tokens(usage: Any, name: str) -> int:
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    return int(value or 0)


def dollars(model: str, usage: Any) -> float:
    """The list price of one call's ``usage``."""
    per_in, per_out = price(model)
    cost = (_tokens(usage, "input_tokens") * per_in
            + _tokens(usage, "cache_creation_input_tokens") * per_in * CACHE_WRITE
            + _tokens(usage, "cache_read_input_tokens") * per_in * CACHE_READ
            + _tokens(usage, "output_tokens") * per_out)
    return cost / 1_000_000


def record(model: str, usage: Any) -> float:
    """Log one call; add it to the job being metered. Never raises."""
    try:
        cost = dollars(model, usage)
        meter = _meter.get()
        if meter is not None:
            meter.append(cost)
        line = {"at": time.time(), "model": model, "usd": round(cost, 6),
                "in": _tokens(usage, "input_tokens"), "out": _tokens(usage, "output_tokens")}
        with _lock, (config.data_dir() / _FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        return cost
    except Exception as exc:  # noqa: BLE001 - a cost note must never break a job
        logger.warning("Couldn't record Claude cost: %s", exc)
        return 0.0


@contextlib.contextmanager
def meter() -> Iterator[list[float]]:
    """Collect the cost of every call made inside; ``sum()`` the list for the total."""
    costs: list[float] = []
    token = _meter.set(costs)
    try:
        yield costs
    finally:
        _meter.reset(token)


def month_total(now: float | None = None) -> float:
    """Dollars spent so far this calendar month, by whoever is asking."""
    month = time.strftime("%Y-%m", time.localtime(now))
    path = config.data_dir() / _FILE
    total = 0.0
    if not path.exists():
        return total
    with _lock, path.open(encoding="utf-8") as f:
        for raw in f:
            try:
                line = json.loads(raw)
                if time.strftime("%Y-%m", time.localtime(line["at"])) == month:
                    total += float(line["usd"])
            except (ValueError, KeyError, TypeError):
                continue
    return total
