"""The My tone dialog: tweaks for every song, gear notes and the suggested starters."""

import json

from fastapi.testclient import TestClient

from stompxl import config, engine, server
from stompxl.db import Library

from .test_engine import FULL, STRAT, FakeClaude
from .test_server import BLOCKS, _wait, client  # noqa: F401  (fixture)


def test_tweaks_can_be_edited_rescoped_paused_and_deleted(client):  # noqa: F811
    strat = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS single coils"}).json()["id"]
    pref = client.post("/api/preferences", json={"text": "cut 420 Hz"}).json()["id"]
    client.put(f"/api/preferences/{pref}", json={"text": "cut 2 dB at 420 Hz", "guitar_id": strat})
    [p] = client.get("/api/state").json()["preferences"]
    assert p["text"] == "cut 2 dB at 420 Hz" and p["guitar_id"] == strat and p["active"] == 1
    client.put(f"/api/preferences/{pref}", json={"active": False})
    [p] = client.get("/api/state").json()["preferences"]
    assert p["active"] == 0 and p["guitar_id"] == strat  # a toggle leaves the scope alone
    client.put(f"/api/preferences/{pref}", json={"guitar_id": None})
    assert client.get("/api/state").json()["preferences"][0]["guitar_id"] is None
    assert client.put(f"/api/preferences/{pref}", json={"text": "  "}).status_code == 400
    client.delete(f"/api/preferences/{pref}")
    assert client.get("/api/state").json()["preferences"] == []


def test_gear_notes_crud_and_validation(client):  # noqa: F811
    nid = client.post("/api/gear-notes", json={"kind": "model", "name": " Scream  808 ",
                                               "text": "Gain 0, Level 8"}).json()["id"]
    [n] = client.get("/api/state").json()["gear_notes"]
    assert n["name"] == "Scream 808" and n["active"] == 1
    client.put(f"/api/gear-notes/{nid}", json={"text": "Gain 1, Level 7", "active": False})
    [n] = client.get("/api/state").json()["gear_notes"]
    assert n["text"] == "Gain 1, Level 7" and n["active"] == 0
    assert client.post("/api/gear-notes", json={"kind": "amp", "name": "x", "text": "y"}).status_code == 400
    assert client.post("/api/gear-notes", json={"kind": "nam", "name": "", "text": "y"}).status_code == 400
    assert client.put("/api/gear-notes/999", json={"text": "y"}).status_code == 404
    client.delete(f"/api/gear-notes/{nid}")
    assert client.get("/api/state").json()["gear_notes"] == []


def test_active_gear_notes_and_tweaks_reach_claude(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = FakeClaude(BLOCKS)
    c = TestClient(server.create_app(Library(tmp_path / "lib.sqlite3"), caller_factory=lambda: fake))
    strat = c.post("/api/guitars", json={"name": "Strat", "pickups": "SSS single coils"}).json()["id"]
    c.post("/api/preferences", json={"text": "Cut 2-3 dB at 420 Hz"})
    c.post("/api/gear-notes", json={"kind": "category", "name": "Reverb", "text": "Low cut 200 Hz"})
    paused = c.post("/api/gear-notes", json={"kind": "model", "name": "Plate Reverb",
                                             "text": "Never above 15%"}).json()["id"]
    c.put(f"/api/gear-notes/{paused}", json={"active": False})
    full = next(r["id"] for r in c.get("/api/state").json()["rigs"] if r["kind"] == "full")
    job = c.post("/api/generate", json={"song": "Song", "guitar_id": strat, "rig_id": full}).json()
    _wait(c, job["job_id"])
    prompt = next(r for r in fake.requests if r.schema_name == "chain").prompt
    assert "Cut 2-3 dB at 420 Hz" in prompt and "any Reverb block: Low cut 200 Hz" in prompt
    assert "Never above 15%" not in prompt


def test_setup_describes_gear_notes_only_for_matching_captures():
    setup = engine.Setup(
        guitar=STRAT, rig={"kind": "nam_pedal", "notes": ""}, options={},
        captures=[{"name": "Dual Rectifier", "slot": 114, "description": "Mesa", "cab": True}],
        gear_notes=[
            {"kind": "nam", "name": "dual rectifier", "text": "Parametric -3 dB at 400 Hz"},
            {"kind": "nam", "name": "Twin Reverb", "text": "clean only"},
            {"kind": "model", "name": "Scream 808", "text": "Gain 0, Level 8"},
            {"kind": "category", "name": "Reverb", "text": "Low cut 200 Hz"},
        ])
    text = setup.describe()
    assert "Dual Rectifier (IR slot 114" in text and "Player's note: Parametric -3 dB at 400 Hz" in text
    assert "clean only" not in text  # that capture isn't on this rig
    assert "Whenever the chain uses 'Scream 808': Gain 0, Level 8" in text
    assert "Whenever the chain uses any Reverb block: Low cut 200 Hz" in text
    # A non-NAM rig gets the block notes but no capture notes.
    plain = engine.Setup(guitar=STRAT, rig=FULL, options={}, gear_notes=setup.gear_notes).describe()
    assert "Parametric -3 dB" not in plain and "Scream 808" in plain
    assert "gear notes" not in engine.Setup(guitar=STRAT, rig=FULL, options={}).describe()


def test_starters_name_real_gear():
    starters = json.loads(server.TONE_STARTERS.read_text())
    cat = engine.catalog()
    models = {m.display_name for m in cat.models()}
    categories = {m.category for m in cat.models()}
    starter_captures = {c["name"] for f in ("nam_starter.json", "nam_full_rig.json")
                        for c in json.loads((server.TONE_STARTERS.parent / f).read_text())["captures"]}
    assert starters["tweaks"] and starters["gear_notes"]
    for note in starters["gear_notes"]:
        pool = {"model": models, "category": categories, "nam": starter_captures}[note["kind"]]
        assert note["name"] in pool, note
    assert len({s["text"] for s in starters["tweaks"] + starters["gear_notes"]}) == \
        len(starters["tweaks"]) + len(starters["gear_notes"])
