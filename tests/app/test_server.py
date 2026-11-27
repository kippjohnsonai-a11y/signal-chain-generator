import time

import pytest
from fastapi.testclient import TestClient

from stompxl import config
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude

BLOCKS = ["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    lib = Library(tmp_path / "lib.sqlite3")
    app = create_app(lib, caller_factory=lambda: FakeClaude(BLOCKS))
    return TestClient(app)


def _wait(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            assert job["status"] == "done", job["error"]
            return job["result"]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_generate_refine_fork_and_download(client):
    state = client.get("/api/state").json()
    assert [r["kind"] for r in state["rigs"]] == ["full", "front", "4cm", "nam_pedal", "nam_usb"]
    strat = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS single coils"}).json()["id"]
    lp = client.post("/api/guitars", json={"name": "Les Paul", "pickups": "HH humbuckers"}).json()["id"]
    full = state["rigs"][0]["id"]

    job = client.post("/api/generate", json={"song": "Enter Sandman by Metallica", "guitar_id": strat,
                                             "rig_id": full}).json()
    first = _wait(client, job["job_id"])
    song = client.get(f"/api/songs/{first['song_id']}").json()
    assert song["title"] == "Enter Sandman"

    version = client.get(f"/api/versions/{first['version_id']}").json()
    assert len(version["view"]["snapshots"]) == 4
    # New presets carry the Stage boost (off) just before the amp.
    assert [b["model"] for b in version["view"]["blocks"][1:3]] == ["Kinky Boost", "Cali Rectifire"]
    assert version["view"]["blocks"][1]["stage_boost"] and not version["view"]["blocks"][1]["enabled"]

    job = client.post(f"/api/versions/{first['version_id']}/refine",
                      json={"feedback": "too muddy", "remember": True, "remember_for_guitar": True}).json()
    second = _wait(client, job["job_id"])
    refined = client.get(f"/api/versions/{second['version_id']}").json()
    assert refined["parent_id"] == first["version_id"] and refined["feedback"] == "too muddy"
    prefs = client.get("/api/state").json()["preferences"]
    assert prefs[0]["text"] == "too muddy" and prefs[0]["guitar_id"] == strat

    job = client.post(f"/api/versions/{second['version_id']}/fork",
                      json={"guitar_id": lp, "rig_id": full,
                            "options": {"wah": False, "expression": False, "looper": False}}).json()
    forked = _wait(client, job["job_id"])
    fork = client.get(f"/api/versions/{forked['version_id']}").json()
    assert fork["guitar_id"] == lp and "Les Paul" in fork["feedback"]

    # Another guitar on the same chain: same blocks, settings re-dialled.
    job = client.post(f"/api/versions/{second['version_id']}/fork",
                      json={"guitar_id": lp, "rig_id": full, "keep_chain": True,
                            "options": {"wah": False, "expression": False, "looper": False}}).json()
    kept = client.get(f"/api/versions/{_wait(client, job['job_id'])['version_id']}").json()
    assert kept["guitar_id"] == lp and kept["label"] == "Les Paul version"
    assert [b["model"] for b in kept["chain"]["blocks"]] == [b["model"] for b in refined["chain"]["blocks"]]
    # Keeping the chain with another rig is refused rather than quietly rebuilt.
    bad = client.post(f"/api/versions/{second['version_id']}/fork",
                      json={"guitar_id": lp, "rig_id": state["rigs"][1]["id"], "keep_chain": True,
                            "options": {"wah": False, "expression": False, "looper": False}})
    assert bad.status_code == 400 and "same chain" in bad.json()["detail"]

    # A fork with nothing changed is a plain copy, no Claude call.
    copy = client.post(f"/api/versions/{second['version_id']}/fork",
                       json={"guitar_id": strat, "rig_id": full, "label": "Live",
                             "options": {"wah": False, "expression": False, "looper": False}}).json()
    assert "version_id" in copy

    hlx = client.get(f"/api/versions/{first['version_id']}/hlx")
    assert hlx.status_code == 200 and b'"device": 2162699' in hlx.content
    assert len(client.get(f"/api/songs/{first['song_id']}").json()["versions"]) == 5


def test_settings_store_key_without_echoing_it(client):
    client.put("/api/settings", json={"anthropic_api_key": "sk-ant-test", "claude_effort": "high"})
    settings = client.get("/api/state").json()["settings"]
    assert settings["has_api_key"] is True and settings["claude_effort"] == "high"
    assert "sk-ant-test" not in str(settings)


def test_index_is_served(client):
    assert "Signal Chain Generator" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_version_endpoint_reports_the_commit(client):
    version = client.get("/api/version").json()
    assert set(version) == {"commit", "date"}
    assert client.get("/api/state").json()["version"] == version


def test_page_is_not_cached_and_assets_carry_the_version(client):
    from stompxl.server import VERSION

    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert f'/static/app.js?v={VERSION["commit"]}' in page.text
    assert f'/static/style.css?v={VERSION["commit"]}' in page.text
    # Every script the page loads gets the stamp, or an update could pair old and new code.
    for script in ("relay", "app", "closeup", "desk", "stage", "help"):
        assert f'/static/{script}.js?v={VERSION["commit"]}' in page.text


def test_page_has_the_walkthrough_and_its_sections(client):
    import re

    page = client.get("/").text
    assert 'id="help-dialog"' in page
    # Every "Show me how" / contents link points at a section that exists.
    sections = set(re.findall(r'id="help-([a-z-]+)"', page))
    for target in set(re.findall(r'data-help="([a-z-]+)"', page)):
        assert target in sections, target
    assert client.get("/static/help.js").status_code == 200


def test_duplicate_rename_and_notes(client):
    state = client.get("/api/state").json()
    strat = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    job = client.post("/api/generate", json={"song": "Enter Sandman by Metallica", "guitar_id": strat,
                                             "rig_id": state["rigs"][0]["id"]}).json()
    first = _wait(client, job["job_id"])
    vid = first["version_id"]

    assert client.put(f"/api/versions/{vid}/notes", json={"text": "Roll the tone knob back in the verse"}).json()["ok"]
    original = client.get(f"/api/versions/{vid}").json()
    assert original["user_notes"] == "Roll the tone knob back in the verse"

    copy = client.post(f"/api/versions/{vid}/duplicate",
                       json={"label": "Live", "preset_name": "Sandman Live"}).json()
    dup = client.get(f"/api/versions/{copy['version_id']}").json()
    assert dup["kind"] == "copy" and dup["label"] == "Live" and dup["parent_id"] == vid
    assert dup["preset_name"] == "Sandman Live"
    assert dup["chain"]["blocks"] == original["chain"]["blocks"]
    assert dup["user_notes"] == original["user_notes"]  # notes follow the copy
    hlx = client.get(f"/api/versions/{copy['version_id']}/hlx").json()
    assert hlx["data"]["meta"]["name"] == "Sandman Live"
    # The original keeps its own name.
    assert client.get(f"/api/versions/{vid}").json()["preset_name"] == original["preset_name"]

    client.put(f"/api/versions/{copy['version_id']}/label",
               json={"label": "Live (loud)", "preset_name": "A name far longer than sixteen"})
    renamed = client.get(f"/api/versions/{copy['version_id']}").json()
    assert renamed["label"] == "Live (loud)" and renamed["preset_name"] == "A name far longe"
    # Renaming only the label leaves the preset name alone.
    client.put(f"/api/versions/{copy['version_id']}/label", json={"label": "Live"})
    assert client.get(f"/api/versions/{copy['version_id']}").json()["preset_name"] == "A name far longe"
    assert client.put(f"/api/versions/{vid}/label", json={"label": "x", "preset_name": "  "}).status_code == 400


def test_first_run_setup_says_what_is_missing_until_done(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("stompxl.helixgen.device.hxedit.find_hx_edit", lambda: None)
    from stompxl import pedal

    monkeypatch.setattr(pedal, "status", lambda: pedal.DeviceStatus(connected=False, error="no libusb"))
    lib = Library(tmp_path / "library.sqlite3")
    client = TestClient(create_app(lib))
    no_pedal = {"connected": False, "description": "", "error": "no libusb"}
    assert client.get("/api/setup").json() == {
        "guest": False, "api_key": False, "api_key_from_env": False, "pedal": no_pedal,
        "guitars": 0, "hx_edit": None, "done": False}
    client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"})
    monkeypatch.setattr("stompxl.helixgen.device.hxedit.find_hx_edit", lambda: tmp_path / "HX Edit.app")
    config.write_settings({"anthropic_api_key": "sk-ant-x"})
    monkeypatch.setattr(pedal, "status", lambda: pedal.DeviceStatus(connected=True, description="HX Stomp XL"))
    client.post("/api/setup/done")
    assert client.get("/api/setup").json() == {
        "guest": False, "api_key": True, "api_key_from_env": False,
        "pedal": {"connected": True, "description": "HX Stomp XL", "error": ""},
        "guitars": 1, "hx_edit": str(tmp_path / "HX Edit.app"), "done": True}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    assert client.get("/api/setup").json()["api_key_from_env"] is True


def test_page_is_utf8_on_every_platform(client):
    page = client.get("/")
    assert "charset=utf-8" in page.headers["content-type"]
    assert "�" not in page.text and "â" not in page.text
