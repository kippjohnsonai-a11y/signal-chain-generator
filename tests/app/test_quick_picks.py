"""Quick picks: the pedal close-up's one-tap moves, kept in the library."""

import pytest
from fastapi.testclient import TestClient

from stompxl import config, quick_picks
from stompxl.db import Library
from stompxl.server import create_app


def test_parse_reads_nudges_sets_and_scales():
    assert quick_picks.parse("Presence -1, LowCut = 100, Decay x0.75, Gain|Drive = 1, "
                             "SyncSelect1 = 1/8 Dotted, Ch Vol +1.5") == [
        {"param": "Presence", "op": "add", "value": -1.0},
        {"param": "LowCut", "op": "set", "value": 100.0},
        {"param": "Decay", "op": "mul", "value": 0.75},
        {"param": "Gain|Drive", "op": "set", "value": 1.0},
        {"param": "SyncSelect1", "op": "set", "value": "1/8 Dotted"},
        {"param": "Ch Vol", "op": "add", "value": 1.5},
    ]


def test_format_round_trips():
    text = "Presence -1, LowCut = 100, Decay x0.75, TempoSync1 = On"
    assert quick_picks.format_changes(quick_picks.parse(text)) == text


@pytest.mark.parametrize("bad", ["", "Presence", "Presence up", "= 3"])
def test_parse_refuses_what_it_cant_read(bad):
    with pytest.raises(quick_picks.PickError):
        quick_picks.parse(bad)


def test_every_starting_pick_parses():
    rows = quick_picks.default_picks()
    assert {r["name"] for r in rows} >= {"Amp", "Cab", "Delay", "Reverb", "EQ"}
    assert any(r["label"] == "420 Hz cut" for r in rows)


def test_a_new_library_gets_the_starting_picks_once(tmp_path):
    path = tmp_path / "lib.sqlite3"
    lib = Library(path)
    start = len(lib.quick_picks())
    assert start == len(quick_picks.default_picks())
    for row in lib.quick_picks():
        lib.delete_quick_pick(row["id"])
    # Deleting them sticks across a restart.
    assert Library(path).quick_picks() == []
    lib.restore_quick_picks("Amp")
    assert {r["name"] for r in lib.quick_picks()} == {"Amp"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    return TestClient(create_app(Library(tmp_path / "lib.sqlite3")))


def test_quick_pick_endpoints(client):
    state = client.get("/api/state").json()
    assert state["quick_main"]["Amp"][0] == "Drive"
    start = len(state["quick_picks"])

    res = client.post("/api/quick-picks", json={"kind": "model", "name": "Cali Rectifire",
                                                "label": "Gig volume", "changes": "ChVol +1"})
    assert res.status_code == 200
    pick_id = res.json()["id"]
    mine = next(q for q in res.json()["quick_picks"] if q["id"] == pick_id)
    assert mine["changes"] == [{"param": "ChVol", "op": "add", "value": 1.0}]

    # From the close-up, changes come already parsed.
    res = client.put(f"/api/quick-picks/{pick_id}",
                     json={"label": "Gig", "changes": [{"param": "ChVol", "op": "set", "value": 7}]})
    mine = next(q for q in res.json()["quick_picks"] if q["id"] == pick_id)
    assert mine["label"] == "Gig" and mine["changes"][0]["op"] == "set"

    bad = client.put(f"/api/quick-picks/{pick_id}", json={"changes": "ChVol up"})
    assert bad.status_code == 400 and "Couldn't read" in bad.json()["detail"]
    assert client.post("/api/quick-picks", json={"kind": "song", "name": "x", "label": "x",
                                                 "changes": "A +1"}).status_code == 400

    assert len(client.delete(f"/api/quick-picks/{pick_id}").json()["quick_picks"]) == start
    amp = [q for q in client.get("/api/state").json()["quick_picks"] if q["name"] == "Amp"]
    client.delete(f"/api/quick-picks/{amp[0]['id']}")
    restored = client.post("/api/quick-picks/restore", json={"category": "Amp"}).json()
    assert len([q for q in restored["quick_picks"] if q["name"] == "Amp"]) == len(amp)
