"""The two NAM rigs: NAM A2 Lite on the pedal, and NAM on the computer over USB."""

import json

import pytest

from stompxl.helixgen.llm import LLMGenerationError
from stompxl import config, desk, edit, engine, importer, xl
from stompxl.db import Library

from .test_engine import STRAT, FakeClaude

CAPTURES = [
    {"name": "JCM800 Crunch", "slot": 12, "description": "Marshall JCM800, amp only", "cab": False},
    {"name": "Recto Full Rig", "slot": 13, "description": "Mesa Rectifier with V30s", "cab": True},
    {"name": "Twin Clean", "slot": None, "description": "computer only", "cab": False},
]
OPTS = {"wah": False, "expression": False, "looper": False, "capo": False}


class NamClaude(FakeClaude):
    def __init__(self, blocks, capture, sides=None):
        super().__init__(blocks)
        self.capture = capture
        self.sides = sides

    def __call__(self, request):
        answer = super().__call__(request)
        if request.schema_name == "chain":
            assert "amp_side" in request.schema["properties"]["blocks"]["items"]["required"]
            data = json.loads(answer)
            data["nam_capture"] = self.capture
            for block, side in zip(data["blocks"], self.sides or [], strict=False):
                block["amp_side"] = side
            return json.dumps(data)
        return answer


def _setup(kind, **options):
    return engine.Setup(guitar=STRAT, rig={"kind": kind, "notes": ""},
                        options={**OPTS, **options},
                        captures=xl.captures_for(kind, {"nam_captures": CAPTURES}))


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))


def test_the_pedal_rig_only_offers_captures_loaded_in_an_ir_slot():
    names = [c["name"] for c in _setup("nam_pedal").captures]
    assert names == ["JCM800 Crunch", "Recto Full Rig"]
    assert len(_setup("nam_usb").captures) == 3
    assert xl.captures_for("full", {"nam_captures": CAPTURES}) == []
    schema = engine._blocks_schema(engine.catalog(), _setup("nam_pedal"))
    assert schema["properties"]["nam_capture"]["enum"] == names
    assert "nam_capture" not in engine._blocks_schema(engine.catalog(), _setup("full"))["properties"]


def test_nam_on_the_pedal_puts_the_capture_slot_on_the_nam_block_before_the_cab():
    fake = NamClaude(["Scream 808", "4x12 Cali V30", "Simple Delay", "Plate Reverb"],
                     "JCM800 Crunch")
    result = engine.generate("Highway to Hell", _setup("nam_pedal"), fake)
    models = [b["model"] for b in result.chain["blocks"]]
    assert models[:3] == ["Scream 808", xl.NAM_MODEL, "4x12 Cali V30"]
    nam = result.chain["blocks"][1]
    assert nam["nam"] and nam["parameters"][xl.NAM_SLOT_PARAMETER] == 12.0
    assert "switch" not in nam  # the amp never gets a stomp switch
    block = result.preset["data"]["tone"]["dsp0"]["block1"]
    assert block["@model"] == "HD2_DistXenomorphFuzz" and block["MinFreq"] == 12.0
    assert block["Gain"] == 0.5 and block["Sensitivity"] == 1.0
    assert result.chain["nam"]["capture"]["name"] == "JCM800 Crunch"
    assert "IR slot 12" in result.notes
    # The snapshots still switch the drive (block0 in Claude's chain, block0 here).
    assert result.preset["data"]["tone"]["snapshot3"]["blocks"]["dsp0"]["block0"] is True


def test_a_nam_preset_read_back_from_the_pedal_shows_as_nam():
    fake = NamClaude(["Scream 808", "4x12 Cali V30", "Plate Reverb"], "JCM800 Crunch")
    sent = engine.generate("Highway to Hell", _setup("nam_pedal"), fake).preset
    captures = xl.parse_captures(CAPTURES)
    chain, _ = importer.hlx_to_chain(sent, captures=captures)
    block = chain["blocks"][1]
    assert block["model"] == xl.NAM_MODEL and block["nam"] is True
    assert block["parameters"][xl.NAM_SLOT_PARAMETER] == 12.0
    assert chain["nam"] == {"mode": "pedal", "capture": captures[0]}
    # A slot that isn't in the player's list is still NAM, named by its slot.
    chain, _ = importer.hlx_to_chain(sent)
    assert chain["blocks"][1]["nam"] and chain["nam"]["capture"]["name"] == "IR slot 12"
    preset, _ = engine.build_preset(chain)
    assert preset["data"]["tone"]["dsp0"]["block1"]["MinFreq"] == 12.0
    # A Min Freq that isn't a whole slot number is a real fuzz's frequency.
    sent["data"]["tone"]["dsp0"]["block1"]["MinFreq"] = 55.5
    chain, _ = importer.hlx_to_chain(sent)
    assert "nam" not in chain["blocks"][1] and "nam" not in chain


def test_the_player_can_say_a_xenomorph_fuzz_is_a_fuzz_or_nam():
    config.write_settings({"nam_captures": CAPTURES})
    fake = NamClaude(["Scream 808", "4x12 Cali V30", "Plate Reverb"], "JCM800 Crunch")
    chain, _ = importer.hlx_to_chain(
        engine.generate("Highway to Hell", _setup("nam_pedal"), fake).preset)
    fuzz, changes = edit.apply_edits(chain, None, [{"block": 1, "nam": False}])
    assert "nam" not in fuzz["blocks"][1] and "nam" not in fuzz
    assert changes == ["Block 2: Xenomorph Fuzz"]
    back, _ = edit.apply_edits(fuzz, None, [{"block": 1, "nam": True}])
    assert back["blocks"][1]["nam"] is True
    assert back["nam"]["capture"]["name"] == "JCM800 Crunch"
    with pytest.raises(edit.EditError):
        edit.apply_edits(chain, None, [{"block": 0, "nam": True}])


def test_the_pedal_pane_shows_nam_with_the_capture_name():
    config.write_settings({"nam_captures": CAPTURES})
    fake = NamClaude(["Scream 808", "4x12 Cali V30", "Plate Reverb"], "JCM800 Crunch")
    opened = desk.from_hlx(engine.generate("Highway to Hell", _setup("nam_pedal"), fake).preset, 3)
    assert opened["chain"]["blocks"][1]["nam"] is True
    assert opened["chain"]["nam"]["capture"]["name"] == "JCM800 Crunch"


def test_nam_on_the_pedal_holds_claude_to_the_capture_and_cab_rules():
    cat = engine.catalog()
    setup = _setup("nam_pedal")
    with pytest.raises(LLMGenerationError, match="NAM is the amp"):
        engine.check_blocks([cat.get("Cali Rectifire"), cat.get("4x12 Cali V30")], setup)
    with pytest.raises(LLMGenerationError, match="app adds the NAM"):
        engine.check_blocks([cat.get(xl.NAM_MODEL)], setup)
    with pytest.raises(LLMGenerationError, match="must come before the amp"):
        engine.check_blocks([cat.get("4x12 Cali V30"), cat.get("Scream 808")], setup)
    with pytest.raises(LLMGenerationError, match="includes its cab"):
        engine._parse_capture("Recto Full Rig", [cat.get("4x12 Cali V30")], setup)
    with pytest.raises(LLMGenerationError, match="amp only"):
        engine._parse_capture("JCM800 Crunch", [cat.get("Simple Delay")], setup)
    with pytest.raises(LLMGenerationError, match="must be one of"):
        engine._parse_capture("Twin Clean", [], setup)
    # The app adds a block, so Claude gets one fewer.
    with pytest.raises(LLMGenerationError, match="choose at most 7"):
        engine.check_blocks([cat.get("Simple Delay")] * 8, setup)


def test_nam_on_the_pedal_needs_captures_before_any_claude_call():
    setup = engine.Setup(guitar=STRAT, rig={"kind": "nam_pedal", "notes": ""}, options=OPTS)
    fake = NamClaude([], "")
    with pytest.raises(LLMGenerationError, match="Which NAMs are on my pedal"):
        engine._build("song", setup, fake, lambda _m: None, engine.catalog(), {"preset_name": "x"},
                      current=None, feedback="")
    assert fake.requests == []


def test_nam_costs_its_dsp_and_the_capo_still_fits_beside_it():
    room = xl.dsp_room("nam_pedal", {"capo": True}, {})
    assert room["used"] == 40 + xl.NAM_DSP + 8
    assert xl.dsp_cost(engine.catalog().get(xl.NAM_MODEL)) == xl.NAM_DSP
    usb = xl.dsp_room("nam_usb", {"capo": True}, {})
    assert usb["used"] == 42 and usb["slots_left"] == 6


def test_nam_on_the_computer_builds_the_usb_sandwich():
    fake = NamClaude(["Scream 808", "Deluxe Comp", "Simple Delay", "Plate Reverb"], "Twin Clean")
    result = engine.generate("Wonderwall", _setup("nam_usb"), fake)
    dsp0 = result.preset["data"]["tone"]["dsp0"]
    blocks = [dsp0[f"block{i}"] for i in range(len(result.chain["blocks"]))]
    names = {m.internal_name: m.display_name for m in engine.catalog().models()}
    assert [(names[b["@model"]], b["@path"], b["@position"]) for b in blocks] == [
        (xl.RETURN_MODEL, 1, 0), ("Scream 808", 1, 1), ("Deluxe Comp", 1, 2),
        (xl.BOOST_MODEL, 0, 0), ("Simple Delay", 0, 1), ("Plate Reverb", 0, 2)]
    assert dsp0["block0"]["@model"] == "HD2_ReturnMono1" and dsp0["block0"]["Mix"] == 1.0
    assert dsp0["block1"]["@path"] == 1 and dsp0["block4"]["@path"] == 0
    assert dsp0["split"]["@position"] == 0
    # The Input block is NAM's return: its gate stays off.
    assert dsp0["inputA"]["noiseGate"] is False
    # Not learnt yet: the preset says what to set by hand.
    assert any("USB 5/6" in w for w in result.warnings)
    assert "Load this capture: Twin Clean, plus a cab IR" in result.notes


def test_nam_on_the_computer_uses_learnt_routing():
    hlx = {"data": {"tone": {"dsp0": {"inputA": {"@input": 7}, "outputB": {"@output": 2}}}}}
    config.write_settings({"nam_usb_routing": xl.learn_usb_routing(hlx)})
    fake = NamClaude(["Scream 808", "Simple Delay"], "a Vox AC30 capture, amp only")
    result = engine.generate("Song", _setup("nam_usb"), fake)
    dsp0 = result.preset["data"]["tone"]["dsp0"]
    assert dsp0["inputA"]["@input"] == 7 and dsp0["outputB"]["@output"] == 2
    assert not any("USB 5/6" in w for w in result.warnings)
    assert result.chain["nam"]["capture"]["suggested"]


def test_learning_refuses_a_preset_not_set_up_for_usb():
    with pytest.raises(ValueError, match="USB 5/6"):
        xl.learn_usb_routing({"data": {"tone": {"dsp0": {"inputA": {"@input": 1},
                                                          "outputB": {"@output": 2}}}}})
    with pytest.raises(ValueError, match="Send L/R"):
        xl.learn_usb_routing({"data": {"tone": {"dsp0": {"inputA": {"@input": 7},
                                                          "outputB": {"@output": 0}}}}})


def test_nam_on_the_computer_keeps_drives_before_the_amp():
    cat = engine.catalog()
    with pytest.raises(LLMGenerationError, match="cab IR runs on the computer"):
        engine.check_blocks([cat.get("4x12 Cali V30")], _setup("nam_usb"))
    with pytest.raises(LLMGenerationError, match="'Scream 808' must come before"):
        engine.check_blocks([cat.get("Simple Delay"), cat.get("Scream 808")], _setup("nam_usb"))


def test_captures_are_cleaned():
    raw = [{"name": " Big ", "slot": "200", "cab": 1}, {"name": "big"}, {"name": ""}, "x",
           {"name": "Small", "slot": "7"}]
    assert xl.parse_captures(raw) == [
        {"name": "Big", "slot": None, "description": "", "cab": True},
        {"name": "Small", "slot": 7, "description": "", "cab": False}]


def test_an_older_library_gains_the_nam_rigs(tmp_path):
    import sqlite3

    from stompxl import db

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(", 'nam_pedal', 'nam_usb'", "")
                       + "INSERT INTO rigs VALUES (1, 'Mine', 'full', '', 0);")
    conn.commit()
    conn.close()
    lib = Library(path)
    assert [r["kind"] for r in lib.rigs()] == ["full", "nam_pedal", "nam_usb"]
    assert [r["kind"] for r in Library(tmp_path / "new.sqlite3").rigs()] == list(xl.RIG_KINDS)


def test_the_app_saves_captures_and_builds_a_usb_preset_for_hx_edit_only(tmp_path):
    from fastapi.testclient import TestClient

    from stompxl.server import create_app

    from .test_server import _wait

    lib = Library(tmp_path / "lib.sqlite3")
    fake = NamClaude(["Scream 808", "Simple Delay"], "Twin Clean")
    client = TestClient(create_app(lib, caller_factory=lambda: fake))
    lines = [{"name": "Twin Clean", "slot": "", "description": "Fender Twin"},
             {"name": "JCM", "slot": 12, "cab": True}]
    assert client.put("/api/settings", json={"nam_captures": lines}).json()["ok"]
    state = client.get("/api/state").json()
    assert [c["name"] for c in state["settings"]["nam_captures"]] == ["Twin Clean", "JCM"]
    assert state["settings"]["nam_usb_routing"] is False

    guitar = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    usb = next(r["id"] for r in state["rigs"] if r["kind"] == "nam_usb")
    job = client.post("/api/generate", json={"song": "Wonderwall", "guitar_id": guitar,
                                             "rig_id": usb}).json()
    done = _wait(client, job["job_id"])
    version = client.get(f"/api/versions/{done['version_id']}").json()
    assert version["view"]["nam"]["capture"]["name"] == "Twin Clean"
    assert [b["path"] for b in version["view"]["blocks"]] == [1, 1, 0, 0]
    refused = client.post(f"/api/versions/{done['version_id']}/push", json={"slot": 3})
    assert refused.status_code == 400 and "HX Edit" in refused.json()["detail"]
    assert client.get(f"/api/versions/{done['version_id']}/hlx").status_code == 200


def test_full_rig_only_offers_captures_with_their_cab_and_frees_the_cab_dsp():
    setup = engine.Setup(guitar=STRAT, rig={"kind": "nam_pedal", "notes": ""},
                         options={**OPTS, "capo": True, "full_rig": True},
                         captures=xl.captures_for("nam_pedal", {"nam_captures": CAPTURES},
                                                  full_rig=True))
    assert [c["name"] for c in setup.captures] == ["Recto Full Rig"]
    schema = engine._blocks_schema(engine.catalog(), setup)
    assert schema["properties"]["nam_capture"]["enum"] == ["Recto Full Rig"]
    assert "Full-rig captures only" in setup.describe()
    with_cab = xl.dsp_room("nam_pedal", {"capo": True}, {})
    without = xl.dsp_room("nam_pedal", {"capo": True, "full_rig": True}, {})
    assert "Cab" not in [r["name"] for r in without["reserved"]]
    assert without["left"] > with_cab["left"] and without["slots_left"] == with_cab["slots_left"] + 1
    fake = NamClaude(["Poly Capo", "Scream 808", "Simple Delay"], "Recto Full Rig")
    result = engine.generate("Here Comes the Sun", setup, fake)
    assert result.chain["nam"]["capture"]["cab"] is True
    assert not any(b["model"].startswith("4x12") for b in result.chain["blocks"])


def test_full_rig_on_the_pedal_with_no_full_rig_captures_says_what_to_do(tmp_path):
    from fastapi.testclient import TestClient

    from stompxl.server import create_app

    lib = Library(tmp_path / "lib.sqlite3")
    client = TestClient(create_app(lib, caller_factory=lambda: NamClaude([], "")))
    client.put("/api/settings", json={"nam_captures": [{"name": "JCM", "slot": 12, "cab": False}]})
    state = client.get("/api/state").json()
    guitar = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    pedal = next(r["id"] for r in state["rigs"] if r["kind"] == "nam_pedal")
    refused = client.post("/api/generate", json={
        "song": "x", "guitar_id": guitar, "rig_id": pedal,
        "options": {"full_rig": True}})
    assert refused.status_code == 400 and "full-rig pack" in refused.json()["detail"]
    room = client.get(f"/api/dsp-room?rig_id={pedal}&full_rig=true").json()
    assert "Cab" not in [r["name"] for r in room["reserved"]]


def test_an_eq_claude_puts_after_the_amp_lands_after_the_nam_block():
    # A full-rig capture has no cab, so nothing used to hold the EQ past NAM.
    fake = NamClaude(["Scream 808", "Parametric", "Simple Delay"], "Recto Full Rig",
                     ["before", "after", "after"])
    result = engine.generate("Song", _setup("nam_pedal", full_rig=True), fake)
    models = [b["model"] for b in result.chain["blocks"]]
    assert models.index(xl.NAM_MODEL) == 1 and models.index("Parametric") == 2
    # ...and the boost still goes past the amp.
    assert models.index(xl.BOOST_MODEL) > models.index(xl.NAM_MODEL)

    # Claude can still put an EQ ahead of the amp, as a pre-EQ.
    fake = NamClaude(["Parametric", "Scream 808", "Simple Delay"], "Recto Full Rig",
                     ["before", "before", "after"])
    result = engine.generate("Song", _setup("nam_pedal", full_rig=True), fake)
    models = [b["model"] for b in result.chain["blocks"]]
    assert models[:3] == ["Parametric", "Scream 808", xl.NAM_MODEL]


def test_amp_sides_must_read_in_order_and_fit_the_block():
    cat = engine.catalog()
    setup = _setup("nam_pedal", full_rig=True)
    eq, drive, delay = cat.get("Parametric"), cat.get("Scream 808"), cat.get("Simple Delay")
    with pytest.raises(LLMGenerationError, match="marked 'before' the amp but comes after"):
        engine.check_blocks([eq, drive], setup, ["after", "before"])
    with pytest.raises(LLMGenerationError, match="'Simple Delay' belongs after the amp"):
        engine.check_blocks([drive, delay], setup, ["before", "before"])
    with pytest.raises(LLMGenerationError, match="'Scream 808' must come before"):
        engine.check_blocks([eq, drive], setup, ["after", "after"])
    engine.check_blocks([drive, eq, delay], setup, ["before", "after", "after"])


def test_nam_on_the_computer_splits_where_claude_marks_the_amp():
    fake = NamClaude(["Scream 808", "Parametric", "Simple Delay"], "Twin Clean",
                     ["before", "after", "after"])
    result = engine.generate("Song", _setup("nam_usb"), fake)
    dsp0 = result.preset["data"]["tone"]["dsp0"]
    names = {m.internal_name: m.display_name for m in engine.catalog().models()}
    paths = {names[dsp0[f"block{i}"]["@model"]]: dsp0[f"block{i}"]["@path"]
             for i in range(len(result.chain["blocks"]))}
    assert paths["Scream 808"] == 1 and paths["Parametric"] == 0


def test_a_tweak_shows_claude_where_the_nam_amp_sits():
    fake = NamClaude(["Scream 808", "Parametric", "Simple Delay"], "Recto Full Rig",
                     ["before", "after", "after"])
    setup = _setup("nam_pedal", full_rig=True)
    first = engine.generate("Song", setup, fake)
    again = NamClaude(["Scream 808", "Parametric", "Simple Delay"], "Recto Full Rig",
                      ["before", "after", "after"])
    engine.refine("Song", setup, again, first.chain, first.brief, "less mud")
    prompt = next(r.prompt for r in again.requests if r.schema_name == "chain")
    assert "1. Scream 808 (before the amp)\n   [NAM amp]\n2. Parametric (after the amp)" in prompt


def test_retune_for_another_guitar_keeps_the_nam_chain_and_redials_the_values():
    fake = NamClaude(["Scream 808", "4x12 Cali V30", "Simple Delay", "Plate Reverb"],
                     "JCM800 Crunch")
    first = engine.generate("Highway to Hell", _setup("nam_pedal"), fake)
    first.chain["blocks"][0]["parameters"] = {"Gain": 0.3, "Tone": 0.5}
    first.chain["snapshots"][3]["blocks"]["0"]["parameters"] = {"Gain": 0.4}

    class Redial(NamClaude):
        def __call__(self, request):
            if request.schema_name == "parameters":
                self.requests.append(request)
                return json.dumps({"blocks": {"block1": {"Gain": 0.6, "Tone": 0.4}}})
            return super().__call__(request)

    les_paul = {**STRAT, "name": "Les Paul", "pickups": "HH humbuckers"}
    setup = engine.Setup(guitar=les_paul, rig={"kind": "nam_pedal", "notes": ""}, options=OPTS,
                         captures=xl.captures_for("nam_pedal", {"nam_captures": CAPTURES}))
    fake2 = Redial([], "unused")
    second = engine.retune("Highway to Hell", setup, fake2, first.chain, first.brief,
                           "Re-dial for the Les Paul (HH humbuckers) instead of the Strat")

    # No block round: Claude never picks blocks, so the chain can't move.
    assert [r.schema_name for r in fake2.requests] == ["parameters", "snapshots"]
    assert [b["model"] for b in second.chain["blocks"]] == [b["model"] for b in first.chain["blocks"]]
    assert second.chain["nam"] == first.chain["nam"]
    assert second.chain["gate"] == first.chain["gate"]
    nam = next(b for b in second.chain["blocks"] if b.get("nam"))
    assert nam["parameters"] == next(b for b in first.chain["blocks"] if b.get("nam"))["parameters"]
    params = next(r.prompt for r in fake2.requests if r.schema_name == "parameters")
    assert "Les Paul" in params and '"Gain": 0.3' in params
    assert "Adaptation: Re-dial for the Les Paul" in params
    assert xl.NAM_MODEL not in params.split("Blocks to configure:")[1]
    assert second.chain["blocks"][0]["parameters"]["Gain"] == 0.6
    # The snapshot round sees the old snapshot values to start from.
    snaps = next(r.prompt for r in fake2.requests if r.schema_name == "snapshots")
    assert '"Gain": 0.4' in snaps
    assert engine.diff_chains(first.chain, second.chain)[0].startswith("Scream 808 Gain")
    assert "Same signal chain" in second.notes


# -- captures already on the pedal (a new computer starts with none listed) ----

def _nam_preset(slot):
    """A NAM-on-the-pedal preset whose NAM block plays IR slot ``slot``."""
    captures = [{"name": "On pedal", "slot": slot, "description": "x", "cab": True}]
    setup = engine.Setup(guitar=STRAT, rig={"kind": "nam_pedal", "notes": ""}, options=OPTS,
                         captures=xl.captures_for("nam_pedal", {"nam_captures": captures}))
    return engine.generate("Wonderwall", setup, NamClaude(["Scream 808"], "On pedal")).preset


def test_saying_a_pack_is_on_the_pedal_lists_it_without_downloading():
    from stompxl import namlib

    config.write_settings({"nam_captures": [{"name": "Mine", "slot": 101, "description": "", "cab": False},
                                            {"name": "Twin Clean", "slot": None}]})
    assert [p["listed"] for p in namlib.pack_status()] == [0, 0, 0]
    captures = namlib.mark_on_pedal(["starter"])
    starter = namlib.pack("starter")
    # The pack's slot 101 capture replaces "Mine" there; the computer-only one stays.
    assert [c["slot"] for c in captures if c["slot"]] == [e["slot"] for e in starter]
    assert "Mine" not in [c["name"] for c in captures] and "Twin Clean" in [c["name"] for c in captures]
    status = {p["name"]: p for p in namlib.pack_status()}
    assert status["starter"]["listed"] == status["starter"]["count"] == 20
    assert (status["starter"]["first_slot"], status["starter"]["last_slot"]) == (101, 120)
    assert status["full-rig"]["listed"] == 0
    # Saying so again changes nothing, and keeps what a download recorded.
    config.write_settings({"nam_captures": [{**c, "tone_id": 1} if c["slot"] == 101 else c for c in captures]})
    again = namlib.mark_on_pedal(["starter"])
    assert next(c for c in again if c["slot"] == 101)["tone_id"] == 1


def test_checking_the_pedal_finds_the_packs_its_presets_use():
    from stompxl import namlib

    presets = {0: ("Back In Black", _nam_preset(104)), 1: ("Clean", _nam_preset(122)),
               2: ("Also 104", _nam_preset(104)), 3: ("Odd one", _nam_preset(7)),
               4: ("Third", _nam_preset(104))}
    found = namlib.nam_on_pedal(presets)
    assert found["presets_read"] == 5
    assert found["packs"] == ["starter", "full-rig"]
    assert [s["slot"] for s in found["slots"]] == [7, 104, 122]
    s104 = next(s for s in found["slots"] if s["slot"] == 104)
    assert s104["presets"] == ["Back In Black", "Also 104", "Third"] and s104["pack"] == "starter"
    assert s104["capture"] == next(e["name"] for e in namlib.pack("starter") if e["slot"] == 104)
    assert found["unknown"] == [7]


def test_generating_nam_on_the_pedal_with_nothing_listed_asks_which_are_on_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from stompxl import pedal
    from stompxl.server import NO_CAPTURES, create_app

    from .test_server import _wait

    lib = Library(tmp_path / "lib.sqlite3")
    client = TestClient(create_app(lib, caller_factory=lambda: NamClaude(["Scream 808"], "Tweed Deluxe")))
    state = client.get("/api/state").json()
    assert [p["listed"] for p in state["settings"]["nam_packs"]] == [0, 0, 0]
    guitar = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    rig = next(r["id"] for r in state["rigs"] if r["kind"] == "nam_pedal")
    body = {"song": "x", "guitar_id": guitar, "rig_id": rig}
    refused = client.post("/api/generate", json=body)
    assert refused.status_code == 409 and refused.json()["detail"] == NO_CAPTURES

    monkeypatch.setattr(pedal, "read_all", lambda report: {5: ("Song", _nam_preset(104))})
    checked = _wait(client, client.post("/api/nam/check-pedal").json()["job_id"])
    assert checked["packs"] == ["starter"]
    assert client.post("/api/nam/on-pedal", json={"packs": ["nope"]}).status_code == 400
    marked = client.post("/api/nam/on-pedal", json={"packs": checked["packs"]}).json()
    assert len(marked["nam_captures"]) == 20 and marked["nam_packs"][0]["listed"] == 20
    done = _wait(client, client.post("/api/generate", json=body).json()["job_id"])
    version = client.get(f"/api/versions/{done['version_id']}").json()
    assert version["view"]["nam"]["capture"]["name"] == "Tweed Deluxe"


def test_every_pack_fits_the_pedals_ir_slots_without_overlapping():
    from stompxl import namlib

    taken: dict[int, str] = {}
    for name in namlib.PACKS:
        for entry in namlib.pack(name):
            assert 1 <= entry["slot"] <= xl.IR_SLOTS, (name, entry)
            assert entry["slot"] not in taken, (name, entry, taken.get(entry["slot"]))
            taken[entry["slot"]] = name
    assert namlib.slot_ranges([121, 122, 123, 44, 45]) == "121-123 and 44-45"
    assert namlib.slot_ranges([51, 52, 53]) == "51-53"
    status = {p["name"]: p for p in namlib.pack_status({})}
    assert status["full-rig"]["slots_text"] == "121-128 and 44-50"
    assert status["extended"]["slots_text"] == "51-100" and status["extended"]["count"] == 50
    # Names stay short enough that HX Edit's 16 characters keep the slot and the name.
    assert all(len(namlib.wav_name(e["slot"], e["name"])) - 4 == len(f"{e['slot']} {e['name']}")
               for e in namlib.pack("extended"))
