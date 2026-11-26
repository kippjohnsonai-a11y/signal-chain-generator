import json

from stompxl.helixgen.device.document import parse
from stompxl import diagnose, engine, xl

from test_device_document import _document
from .test_engine import FakeClaude, _setup


def _models(*names):
    cat = engine.catalog()
    return [cat.get(n) for n in names]


def test_looper_on_six_then_bottom_row_by_priority():
    models = _models("Fassel", "Scream 808", "Cali Rectifire", "4x12 Cali V30", "70s Chorus",
                     "Simple Delay", "Gain", xl.LOOPER_MODEL)
    plan = xl.plan_footswitches(models)
    assert plan[7] == 6                      # looper
    assert plan[0] == 5                      # wah
    assert plan[1] == 2                      # drive
    assert plan[5] == 3                      # delay
    assert 6 not in plan and 4 not in plan   # boost and chorus: no switch left
    assert set(plan.values()) <= {2, 3, 5, 6}  # FS1/FS4 are snapshot up/down


def test_without_looper_fs6_is_a_stomp_too():
    models = _models("Scream 808", "Cali Rectifire", "4x12 Cali V30", "Gain", "Simple Delay")
    plan = xl.plan_footswitches(models)
    assert plan == {0: 5, 4: 2, 3: 3}
    models = _models("Scream 808", "Cali Rectifire", "4x12 Cali V30", "70s Chorus", "Gain",
                     "Simple Delay")
    assert xl.plan_footswitches(models)[3] == 6    # chorus, fourth in line


def test_generated_preset_gets_boost_and_planned_switches_only():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Simple Delay",
                       "Plate Reverb", xl.LOOPER_MODEL])
    result = engine.generate("Song", _setup(looper=True), fake)
    names = [b["model"] for b in result.chain["blocks"]]
    assert names == ["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Gain", "Simple Delay",
                     "Plate Reverb", xl.LOOPER_MODEL]
    tone = result.preset["data"]["tone"]
    boost = names.index("Gain")
    assert tone["dsp0"][f"block{boost}"]["Gain"] == xl.BOOST_DB
    assert all(tone[f"snapshot{i}"]["blocks"]["dsp0"][f"block{boost}"] is False for i in range(4))
    assert all(tone[f"snapshot{i}"]["blocks"]["dsp0"]["block6"] is True for i in range(4))
    fs = {k: (v["@fs_index"], v["@fs_label"]) for k, v in tone["footswitch"]["dsp0"].items()}
    assert fs["block6"] == (6, "LOOPER")
    assert fs["block0"][0] == 5                     # drive
    assert fs["block4"][0] == 2                     # delay
    assert fs[f"block{boost}"] == (3, "BOOST")
    assert "block1" not in fs and "block2" not in fs


def test_full_chain_has_no_room_for_boost():
    fake = FakeClaude(["Fassel", "Scream 808", "Cali Rectifire", "4x12 Cali V30", "70s Chorus",
                       "Simple Delay", "Plate Reverb", xl.LOOPER_MODEL])
    result = engine.generate("Song", _setup(wah=True, looper=True), fake)
    assert "Gain" not in [b["model"] for b in result.chain["blocks"]]


def test_switch_map_override():
    assert xl.switch_map({"footswitch_map": {"6": 9}})[6] == 9


def test_diagnose_describes_a_document():
    report = diagnose.describe(parse(_document()))
    json.dumps(report, default=repr)
    assert report["footswitches"] and report["snapshots"]


def test_resolve_slot_by_label_name_or_number():
    import pytest

    names = {113: "Test1", 5: "Clean"}
    assert diagnose.resolve_slot("29B", names) == 113
    assert diagnose.resolve_slot("32D", names) == 127
    assert diagnose.resolve_slot("1a", names) == 0
    assert diagnose.resolve_slot("test1", names) == 113
    assert diagnose.resolve_slot("Test 1", {113: "Test1"}) == 113
    assert diagnose.resolve_slot("7", names) == 7
    with pytest.raises(diagnose.SlotNotFound):
        diagnose.resolve_slot("Nope", names)


def test_xl_snapshot_controllers_use_source_12():
    from stompxl.helixgen.device.document import snapshot_source

    assert snapshot_source(0x4253) == 12
    assert snapshot_source(0x4246) == 9

    doc = parse(_document())
    group = doc.preset[4]
    group.extend([None] * (13 - len(group)))
    doc.clear_controllers()
    count = len(doc.snapshots())
    assigned = doc.add_snapshot_controller(slot=1, parameter=0, model_sel=0, minimum=0.0,
                                           maximum=1.0, values=[0.5] * count, source=12)
    assert assigned is not None
    assert doc.preset[4][12][0][1][0] == 12 and not doc.preset[4][9]


def test_generated_xl_preset_marks_snapshot_controllers_12():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    first = engine.generate("Song", _setup(), fake)
    chain = first.chain
    chain["snapshots"][3]["blocks"]["1"] = {"parameters": {"Drive": 0.9}}
    preset, _ = engine.build_preset(chain)
    sources = {e["@controller"] for p in preset["data"]["tone"]["controller"]["dsp0"].values()
               for e in p.values()}
    assert sources == {xl.CONTROLLER_SNAPSHOT}


def test_snapshots_carry_the_song_tempo():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    tone = engine.generate("Song", _setup(), fake).preset["data"]["tone"]
    assert {tone[f"snapshot{i}"]["@tempo"] for i in range(4)} == {123}


def test_diagnose_dumps_the_input_node():
    report = diagnose.describe(parse(_document()))
    assert any(n["tag"] == 1 for n in report["nodes"])


def test_diagnose_lists_controller_assignments_readably():
    doc = parse(_document())
    group = doc.preset[4]
    group.extend([None] * (13 - len(group)))
    doc.clear_controllers()
    doc.add_snapshot_controller(slot=1, parameter=0, model_sel=0, minimum=0.0, maximum=1.0,
                                values=[0.5] * len(doc.snapshots()), source=12)
    rows = diagnose.controller_assignments(doc)
    assert rows[0]["source"] == 12 and rows[0]["source_label"] == "Snapshots"
    assert rows[0]["block_slot"] == 1 and "slot 1" in rows[0]["summary"]


def test_deep_diff_finds_nested_changes():
    from stompxl.diagnose import deep_diff

    a = {1: {7: {4: [0.5, 0.0]}}, 2: b"Gate On\x00\x00", 3: [1, 2]}
    b = {1: {7: {4: [0.5, 0.25]}}, 2: b"Gate Off\x00", 3: [1, 2, 3], 4: True}
    diffs = {d["path"]: (d["first"], d["other"]) for d in deep_diff(a, b)}
    assert diffs == {"/1/7/4/1": (0.0, 0.25), "/2": ("Gate On", "Gate Off"),
                     "/3": ([1, 2], [1, 2, 3]), "/4": (None, True)}


def test_boost_and_looper_ignore_snapshots():
    """Changing snapshot mustn't switch off a stomped boost or a playing looper."""
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb", xl.LOOPER_MODEL])
    result = engine.generate("Enter Sandman", _setup(looper=True), fake)
    dsp0 = result.preset["data"]["tone"]["dsp0"]
    flags = {b["model"]: dsp0[f"block{i}"]["@no_snapshot_bypass"]
             for i, b in enumerate(result.chain["blocks"])}
    assert flags[xl.LOOPER_MODEL] is True and flags[xl.BOOST_MODEL] is True
    assert flags["Scream 808"] is False


def test_stage_boost_sits_off_before_the_amp_with_no_footswitch():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    result = engine.generate("Song", _setup(stage_boost=True), fake)
    blocks = result.chain["blocks"]
    names = [b["model"] for b in blocks]
    assert names[:4] == ["Scream 808", xl.STAGE_BOOST_MODEL, "Cali Rectifire", "4x12 Cali V30"]
    at = names.index(xl.STAGE_BOOST_MODEL)
    assert blocks[at]["stage_boost"] is True
    tone = result.preset["data"]["tone"]
    assert tone["dsp0"][f"block{at}"]["@no_snapshot_bypass"] is True
    assert tone["dsp0"][f"block{at}"]["@enabled"] is False
    assert f"block{at}" not in tone["footswitch"]["dsp0"]
    assert tone["footswitch"]["dsp0"]["block0"]["@fs_index"] == 5   # the drive keeps its switch


def test_stage_boost_left_out_when_the_chain_is_full():
    fake = FakeClaude(["Fassel", "Scream 808", "Cali Rectifire", "4x12 Cali V30", "70s Chorus",
                       "Simple Delay", "Plate Reverb", xl.LOOPER_MODEL])
    result = engine.generate("Song", _setup(wah=True, looper=True, stage_boost=True), fake)
    assert xl.STAGE_BOOST_MODEL not in [b["model"] for b in result.chain["blocks"]]


def test_stage_boost_can_be_added_to_an_existing_preset():
    from stompxl import edit, view

    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    result = engine.generate("Song", _setup(), fake)
    chain, changes = edit.apply_edits(result.chain, result.preset, [{"stage_boost": True}])
    at = [b["model"] for b in chain["blocks"]].index(xl.STAGE_BOOST_MODEL)
    assert chain["blocks"][at + 1]["model"] == "Cali Rectifire"
    assert "Stage boost" in changes[0]
    preset, _ = engine.build_preset(chain)
    shown = view.preset_view(chain, preset)
    assert shown["blocks"][at]["stage_boost"] and shown["blocks"][at]["stomp"]
    assert shown["blocks"][at]["enabled"] is False
    assert shown["playing_snapshot"] == 0
    import pytest
    with pytest.raises(edit.EditError):
        edit.apply_edits(chain, preset, [{"stage_boost": True}])
