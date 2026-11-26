"""The pedal pane: a preset straight off the pedal, tuned live, no song needed."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from stompxl.helixgen.device.editor import OP_SAVE_PRESET, OP_SET_VALUE
from stompxl.helixgen.llm import LLMRequest
from stompxl import assistant, config, desk, engine, live, pedal, view
from stompxl.claude import sanitize_schema
from stompxl.db import Library
from stompxl.server import create_app

from .test_live import FakeRun, _preset, fakes  # noqa: F401 - fixture

READS: list[int] = []


class ChatClaude:
    """Answers the pedal chat with one real suggestion and one made-up change."""

    def __init__(self):
        self.requests: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> str:
        self.requests.append(request)
        json.dumps(sanitize_schema(request.schema))
        assert request.schema_name == "pedal_chat"
        return json.dumps({"reply": "Back the drive off a touch.", "suggestions": [
            {"label": "Less fizz", "why": "Less clipping.", "changes": [
                {"block": 0, "parameter": "Gain", "value": "3.5"},
                {"block": 0, "parameter": "NotAKnob", "value": "1"},
                {"block": 2, "parameter": "@enabled", "value": "off"}]},
            {"label": "Nonsense", "why": "", "changes": [
                {"block": 9, "parameter": "Gain", "value": "1"}]}]})


@pytest.fixture
def client(tmp_path, monkeypatch, fakes):  # noqa: F811 - the fixture imported above
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(live, "_make", lambda v, p, s, b: live.LiveSession(
        version_id=v, preset=p, slot=s, symbols=None, amps=None, catalog=engine.catalog(),
        open_run=FakeRun))
    READS.clear()
    pedal._listings.clear()

    def read(slot, bank=0):
        READS.append(slot)
        hlx = _preset()
        hlx["data"]["meta"]["name"] = f"Slot {slot}"
        return hlx

    monkeypatch.setattr(pedal, "read_preset", read)
    pushes = []
    monkeypatch.setattr(pedal, "push", lambda preset, slot, **kw: pushes.append(slot) or "ok")
    lib = Library(tmp_path / "lib.sqlite3")
    claude = ChatClaude()
    app = create_app(lib, caller_factory=lambda: claude)
    yield TestClient(app), lib, claude, pushes
    live.stop(keep=False)
    desk.forget()


def _wait(http, job_id):
    for _ in range(200):
        job = http.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            assert job["status"] == "done", job["error"]
            return job["result"]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_opens_a_slot_live_without_making_a_song(client):
    http, lib, _, _ = client
    assert http.get("/api/desk").json() == {"version": None, "live": None, "slots": None}
    res = http.post("/api/desk/open", json={"slot": 5})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["live"] == {"slot": 5, "label": "2B"} and body["live_error"] == ""
    assert body["version"]["id"] == desk.DESK_ID and body["version"]["pedal"] is True
    assert body["version"]["preset_name"] == "Slot 5"
    assert [b["model"] for b in body["version"]["view"]["blocks"]] == [
        "Scream 808", "Cali Rectifire", "Plate Reverb"]
    assert lib.songs() == []

    # The usual version endpoints work on it: live value edits, preview, Explain.
    edit = {"block": 0, "parameter": "Gain", "value": 9}
    res = http.post(f"/api/versions/{desk.DESK_ID}/live/update", json={"edits": [edit]})
    assert res.status_code == 200 and res.json()["sent"] == 1
    res = http.post(f"/api/versions/{desk.DESK_ID}/preview", json={"edits": [edit]})
    assert res.status_code == 200

    # Jumping to another preset lets the first go and reads the new one.
    res = http.post("/api/desk/open", json={"slot": 6}).json()
    assert res["live"]["label"] == "2C" and READS == [5, 6]


def test_save_keeps_value_edits_on_the_slot_and_stays_live(client):
    http, _, _, pushes = client
    http.post("/api/desk/open", json={"slot": 5})
    edit = {"block": 0, "parameter": "Gain", "value": 9}
    res = http.post("/api/desk/save", json={"edits": [edit]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["how"] == "live" and body["live"]["label"] == "2B" and pushes == []
    sent = [op for op, _ in FakeRun.opened[-1].session.sent]
    assert OP_SET_VALUE in sent and sent[-1] == OP_SAVE_PRESET
    gain = next(p for p in body["version"]["view"]["blocks"][0]["params"] if p["param"] == "Gain")
    assert gain["value"] == "9.0"
    # The saved value is the new starting point: nothing more to send.
    res = http.post(f"/api/versions/{desk.DESK_ID}/live/update", json={"edits": []})
    assert res.json()["sent"] == 0


def test_save_with_a_block_change_writes_the_whole_preset(client):
    http, _, _, pushes = client
    http.post("/api/desk/open", json={"slot": 5})
    res = http.post("/api/desk/save", json={"edits": [{"remove": 2}]}).json()
    result = _wait(http, res["job_id"])
    assert pushes == [5] and result["how"] == "push"
    assert len(result["version"]["view"]["blocks"]) == 2


def test_save_as_song_puts_it_in_the_library(client):
    http, lib, _, _ = client
    http.post("/api/desk/open", json={"slot": 5})
    guitar = http.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    rig = http.get("/api/state").json()["rigs"][0]["id"]
    res = http.post("/api/desk/song", json={"title": "Gig tone", "guitar_id": guitar, "rig_id": rig,
                                            "edits": [{"block": 0, "parameter": "Gain", "value": 7}]})
    assert res.status_code == 200, res.text
    assert [s["title"] for s in lib.songs()] == ["Gig tone"]


def test_chat_offers_only_changes_the_chain_can_take(client):
    http, lib, claude, _ = client
    lib.add_preference("Cut 420 Hz by 4-5 dB")
    http.post("/api/desk/open", json={"slot": 5})
    res = http.post("/api/desk/chat", json={"messages": [{"role": "user", "text": "Too fizzy"}]})
    answer = _wait(http, res.json()["job_id"])
    assert answer["reply"] == "Back the drive off a touch."
    assert [s["label"] for s in answer["suggestions"]] == ["Less fizz"]
    changes = answer["suggestions"][0]["changes"]
    assert changes[0] == {"block": 0, "param": "Gain", "name": "Gain", "value": "3.5",
                          "label": "Scream 808 Gain 3.5"}
    assert changes[1]["enabled"] is False and answer["dropped"] == 2
    prompt = claude.requests[0].prompt
    assert "Cut 420 Hz" in prompt and "Too fizzy" in prompt and "Block 1: Cali Rectifire" in prompt


def test_check_change_clamps_and_matches_options():
    v = view.preset_view(*_chain_and_preset())
    blk = v["blocks"][0]
    gain = next(p for p in blk["params"] if p["param"] == "Gain")
    high = assistant.check_change(v, {"block": 0, "parameter": "gain", "value": "99"})
    assert high["value"] == f"{gain['edit']['max']:g}"
    assert assistant.check_change(v, {"block": 0, "parameter": "Gain", "value": "loud"}) is None


def _chain_and_preset():
    from .test_live import CHAIN

    return CHAIN, _preset(CHAIN)
