"""The HX Stomp alongside the HX Stomp XL: one profile per pedal."""

import json

import pytest
from fastapi.testclient import TestClient

from stompxl import config, engine, knowledge, pedal, xl

from .test_engine import FakeClaude, _brief, _setup


@pytest.fixture
def stomp() -> None:
    config.write_settings({"pedal_model": "stomp"})


class StompClaude(FakeClaude):
    """Answers with three sections and three snapshots, as the Stomp asks for."""

    def __call__(self, request):
        if request.schema_name == "brief":
            self.requests.append(request)
            brief = _brief()
            brief["sections"] = brief["sections"][:3]
            return json.dumps(brief)
        if request.schema_name == "snapshots":
            self.requests.append(request)
            keys = request.schema["properties"]["snapshots"]["items"]["properties"]["blocks"]["required"]
            return json.dumps({"snapshots": [
                {"name": name, "blocks": {k: {"enabled": None} for k in keys}}
                for name in ("VERSE", "CHORUS", "SOLO")]})
        return super().__call__(request)


def test_the_stomp_xl_is_the_default():
    assert xl.pedal().key == "stomp_xl"
    assert (xl.SNAPSHOT_COUNT, xl.SLOT_COUNT, xl.DEVICE_ID) == (4, 128, 2162699)
    tone = xl.template()["data"]["tone"]
    assert sorted(k for k in tone if k.startswith("snapshot")) == [f"snapshot{i}" for i in range(4)]
    assert pedal.slot_label(4) == "2A"


def test_the_stomp_profile(stomp):
    assert xl.PEDAL_NAME == "HX Stomp"
    assert (xl.SNAPSHOT_COUNT, xl.SLOT_COUNT, xl.BANK_SIZE, xl.DEVICE_ID) == (3, 126, 3, 2162694)
    assert xl.CONTROLLER_SNAPSHOT == 9
    data = xl.template()["data"]
    assert data["device"] == 2162694
    assert sorted(k for k in data["tone"] if k.startswith("snapshot")) == ["snapshot0", "snapshot1",
                                                                           "snapshot2"]
    # 42 banks of three: 01A-42C.
    assert [pedal.slot_label(s) for s in (0, 2, 3, 125)] == ["1A", "1C", "2A", "42C"]
    assert xl.switch_map() == {1: 1, 2: 2, 3: 3}


def test_stomp_switches_put_the_looper_on_fs3(stomp):
    cat = engine.catalog()
    models = [cat.get(m) for m in ("Scream 808", "Cali Rectifire", "Simple Delay", "Plate Reverb",
                                   xl.LOOPER_MODEL)]
    plan = xl.plan_footswitches(models)
    assert plan[4] == 3
    assert sorted(plan.values()) == [1, 2, 3]


def test_knowledge_follows_the_pedal(stomp):
    text = knowledge.library_text()
    assert "# HX Stomp platform limits" in text
    assert "**3 snapshots**" in text and "Snapshot mode" in text
    assert "{{" not in text
    config.write_settings({"pedal_model": "stomp_xl"})
    xl_text = knowledge.library_text()
    assert "# HX Stomp XL platform limits" in xl_text and "SNAP up (FS1)" in xl_text
    assert "{{" not in xl_text


def test_generate_makes_a_three_snapshot_stomp_preset(stomp):
    fake = StompClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Simple Delay", "Plate Reverb"])
    result = engine.generate("Enter Sandman by Metallica", _setup(), fake)
    data = result.preset["data"]
    assert data["device"] == 2162694
    tone = data["tone"]
    assert "snapshot3" not in tone
    assert [tone[f"snapshot{i}"]["@name"] for i in range(3)] == ["VERSE", "CHORUS", "SOLO"]
    brief_prompt = fake.requests[0].prompt
    assert "HX Stomp preset" in brief_prompt and "Exactly 3 sections" in brief_prompt
    assert all(b.get("switch") in (None, 1, 2, 3) for b in result.chain["blocks"])


def test_an_xl_song_sent_to_a_stomp_is_rebuilt():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Simple Delay", "Plate Reverb",
                       xl.LOOPER_MODEL])
    made = engine.generate("Enter Sandman by Metallica", _setup(looper=True), fake)
    assert made.preset["data"]["device"] == 2162699
    same, notes = engine.for_current_pedal(made.chain, made.preset)
    assert same is made.preset and notes == []

    config.write_settings({"pedal_model": "stomp"})
    rebuilt, notes = engine.for_current_pedal(made.chain, made.preset)
    assert rebuilt["data"]["device"] == 2162694
    assert "snapshot3" not in rebuilt["data"]["tone"]
    assert notes[0] == "Rebuilt for the HX Stomp."
    assert any("Snapshot 4 (SOLO) left off" in n for n in notes)
    switches = rebuilt["data"]["tone"].get("footswitch", {}).get("dsp0", {})
    assert switches and all(1 <= fs["@fs_index"] <= 3 for fs in switches.values())


def test_plugging_in_a_pedal_switches_the_profile():
    assert xl.remember_pedal(0x4246).key == "stomp"
    assert config.get_setting("pedal_model") == "stomp"
    assert xl.remember_pedal(0x4253).key == "stomp_xl"
    assert xl.pedal().key == "stomp_xl"
    assert xl.remember_pedal(0x4248) is None  # a Helix Floor: left as it was
    assert xl.pedal().key == "stomp_xl"


def test_settings_offer_and_check_the_pedal():
    from stompxl.server import create_app

    client = TestClient(create_app())
    state = client.get("/api/state").json()
    assert state["settings"]["pedal_model"] == "stomp_xl"
    assert state["settings"]["pedal_models"] == {"stomp_xl": "HX Stomp XL", "stomp": "HX Stomp"}
    assert state["pedal"]["bank_size"] == 4
    assert client.put("/api/settings", json={"pedal_model": "helix"}).status_code == 400
    assert client.put("/api/settings", json={"pedal_model": "stomp"}).status_code == 200
    state = client.get("/api/state").json()
    assert (state["slot_count"], state["pedal"]["snapshot_count"], state["pedal"]["name"]) == (
        126, 3, "HX Stomp")
