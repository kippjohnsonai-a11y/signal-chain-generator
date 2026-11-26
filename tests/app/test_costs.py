"""Claude's cost: per call, per job, and for the month."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from stompxl.helixgen.llm import LLMGenerationError, LLMRequest
from stompxl import claude, config, costs
from stompxl.db import Library
from stompxl.server import create_app

from .test_claude import REQUEST, _client


def _usage(i=0, o=0, write=0, read=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=write, cache_read_input_tokens=read)


def test_prices_by_model():
    assert costs.price("claude-opus-5-5") == (4.0, 20.0)
    assert costs.price("claude-opus-5") == (5.0, 25.0)
    assert costs.price("claude-sonnet-5-5-20261101") == (2.0, 10.0)
    assert costs.price("claude-haiku-4-5") == (1.0, 5.0)
    assert costs.price("claude-fable-5-1") == (10.0, 50.0)
    assert costs.price("claude-fable-5-2") == (8.0, 40.0)
    assert costs.price("claude-something-new") == costs.DEFAULT_PRICE


def test_dollars_counts_cache_reads_and_writes():
    # 1M in at $4, 100k out at $20, 1M cache read at $0.40, 1M cache write at $5.
    usd = costs.dollars("claude-opus-5-5", _usage(1_000_000, 100_000, 1_000_000, 1_000_000))
    assert usd == pytest.approx(4 + 2 + 5 + 0.4)
    assert costs.dollars("claude-opus-5-5", None) == 0


def test_the_caller_records_each_call_and_the_month_adds_up():
    reply = SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5-5", usage=_usage(10_000, 2_000),
                            content=[SimpleNamespace(type="text", text='{"x": 1}')])
    client, _ = _client([reply, reply])
    call = claude.build_caller(model="claude-opus-5-5", client=client)
    with costs.meter() as spent:
        call(REQUEST)
        call(REQUEST)
    # Priced at the model that answered (a fallback can differ from the one asked for).
    assert sum(spent) == pytest.approx(2 * (0.02 + 0.02))
    assert costs.month_total() == pytest.approx(0.08)
    # Last month's calls don't count.
    with (config.data_dir() / "claude-costs.jsonl").open("a") as f:
        f.write(json.dumps({"at": time.time() - 40 * 86400, "usd": 5}) + "\n")
        f.write("not json\n")
    assert costs.month_total() == pytest.approx(0.08)


def test_no_key_says_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    error = TypeError('"Could not resolve authentication method. Expected one of api_key"')
    client, _ = _client([error])
    call = claude.build_caller(model="claude-opus-5-5", client=client)
    with pytest.raises(LLMGenerationError, match="Add an Anthropic API key"):
        call(REQUEST)


def test_a_job_reports_its_cost_and_settings_the_month(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def caller():
        def call(request: LLMRequest) -> str:
            costs.record("claude-opus-5-5", _usage(100_000, 10_000))  # $0.60
            raise LLMGenerationError("stop here")
        return call

    lib = Library(tmp_path / "library.sqlite3")
    client = TestClient(create_app(lib, caller_factory=caller))
    client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"})
    state = client.get("/api/state").json()
    assert state["settings"]["has_api_key"] is False
    assert state["settings"]["claude_cost_month"] == 0
    job_id = client.post("/api/generate", json={
        "song": "Song", "guitar_id": lib.guitars()[0]["id"],
        "rig_id": state["rigs"][0]["id"]}).json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "error" and job["cost_usd"] == pytest.approx(0.6)
    assert client.get("/api/state").json()["settings"]["claude_cost_month"] == pytest.approx(0.6)
