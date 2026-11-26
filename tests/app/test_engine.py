import json

import pytest

from stompxl.helixgen.llm import LLMGenerationError, LLMRequest
from stompxl import engine, xl
from stompxl.claude import sanitize_schema

STRAT = {"name": "Fender Stratocaster", "pickups": "SSS single coils", "pickup_notes": "",
         "tuning": "E standard", "notes": ""}
FULL = {"kind": "full", "notes": ""}


def _brief():
    return {
        "title": "Enter Sandman", "artist": "Metallica", "preset_name": "Enter Sandman",
        "tuning": "E standard", "tempo_bpm": 123, "reference_gear": "Mesa Mark IIC+",
        "tone_summary": "Tight scooped high gain", "guitar_adaptation": "More gain for SSS",
        "sections": [
            {"name": n, "role": n, "description": "", "pickup": "bridge", "guitar_knobs": "10"}
            for n in ("intro", "verse", "chorus", "solo")
        ],
        "wah": {"recommended": True, "how": "solo"},
        "expression": {"recommended": True, "how": "wah"},
        "looper": {"recommended": False, "how": ""},
        "playing_tips": ["palm mute"],
    }


class FakeClaude:
    def __init__(self, blocks):
        self.blocks = blocks
        self.requests: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> str:
        self.requests.append(request)
        # Every schema must be accepted by structured outputs once sanitized.
        json.dumps(sanitize_schema(request.schema))
        if request.schema_name == "brief":
            return json.dumps(_brief())
        if request.schema_name == "chain":
            return json.dumps({"interpretation": "Tighten it.",
                               "blocks": [{"model": m, "why": "because"} for m in self.blocks]})
        if request.schema_name == "parameters":
            keys = request.schema["properties"]["blocks"]["required"]
            return json.dumps({"blocks": {k: {} for k in keys}})
        if request.schema_name == "snapshots":
            keys = request.schema["properties"]["snapshots"]["items"]["properties"]["blocks"]["required"]
            snaps = []
            for name in ("INTRO", "VERSE", "CHORUS", "SOLO"):
                blocks = {k: {"enabled": None} for k in keys}
                if name == "SOLO":
                    blocks["block1"] = {"enabled": True}
                else:
                    blocks["block1"] = {"enabled": False}
                snaps.append({"name": name, "blocks": blocks})
            return json.dumps({"snapshots": snaps})
        if request.schema_name == "explain":
            return json.dumps({"summary": "A plate reverb.", "role": "Space behind the riff.",
                               "parameters": [{"name": "Mix", "what": "Wet level.", "how": "10-30%.",
                                               "recommended": "20%", "why": "Keeps it tight."}],
                               "tips": ["Keep it last."]})
        raise AssertionError(request.schema_name)


def _setup(**options):
    opts = {"wah": False, "expression": False, "looper": False, **options}
    return engine.Setup(guitar=STRAT, rig=FULL, options=opts, preferences=["No chorus ever"])


def test_generate_builds_a_four_snapshot_xl_preset():
    fake = FakeClaude(["Fassel", "Scream 808", "Cali Rectifire", "4x12 Cali V30", "Simple Delay",
                       "Plate Reverb", xl.LOOPER_MODEL])
    result = engine.generate("Enter Sandman by Metallica",
                             _setup(wah=True, expression=True, looper=True), fake)
    data = result.preset["data"]
    assert data["device"] == xl.DEVICE_ID
    tone = data["tone"]
    assert [tone[f"snapshot{i}"]["@name"] for i in range(4)] == ["INTRO", "VERSE", "CHORUS", "SOLO"]
    assert tone["snapshot3"]["blocks"]["dsp0"]["block0"] is True
    assert tone["snapshot0"]["blocks"]["dsp0"]["block0"] is False
    assert tone["controller"]["dsp0"]["block0"]["Pedal"]["@controller"] == xl.CONTROLLER_EXP1
    assert tone["footswitch"]["dsp0"]["block0"]["@fs_label"] == "WAH"
    assert tone["global"]["@tempo"] == 123
    # Standing instructions and the knowledge library reach the prompts.
    assert "No chorus ever" in fake.requests[0].prompt
    assert "Tailoring to the guitar" in fake.requests[0].prompt


def test_rig_rules_reject_an_amp_in_front_of_a_real_amp():
    setup = engine.Setup(guitar=STRAT, rig={"kind": "front", "notes": ""},
                         options={"wah": False, "expression": False, "looper": False})
    cat = engine.catalog()
    with pytest.raises(LLMGenerationError, match="real amp"):
        engine.check_blocks([cat.get("Cali Rectifire"), cat.get("4x12 Cali V30")], setup)


def test_toggles_are_enforced():
    cat = engine.catalog()
    chain = [cat.get("Cali Rectifire"), cat.get("4x12 Cali V30")]
    with pytest.raises(LLMGenerationError, match="wah toggle is ON"):
        engine.check_blocks(chain, _setup(wah=True))
    with pytest.raises(LLMGenerationError, match="looper"):
        engine.check_blocks([*chain, cat.get(xl.LOOPER_MODEL)], _setup())
    with pytest.raises(LLMGenerationError, match="last block"):
        engine.check_blocks([cat.get(xl.LOOPER_MODEL), *chain], _setup(looper=True))


def test_refine_starts_from_current_values_and_reports_feedback():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    first = engine.generate("Enter Sandman", _setup(), fake)
    first.chain["blocks"][1]["parameters"] = {"Drive": 0.7}

    fake2 = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    second = engine.refine("Enter Sandman", _setup(), fake2, first.chain, first.brief,
                           "this sounds too muddy")
    params_prompt = next(r.prompt for r in fake2.requests if r.schema_name == "parameters")
    assert "too muddy" in params_prompt
    assert '"Drive": 0.7' in params_prompt
    assert second.preset["data"]["device"] == xl.DEVICE_ID
    changes = engine.diff_chains(first.chain, second.chain)
    assert any("Drive" in line for line in changes)


def test_sanitize_schema_turns_nullable_enums_into_anyof():
    out = sanitize_schema({"type": ["string", "null"], "enum": ["a", None]})
    assert out == {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "null"}]}
    out = sanitize_schema({"type": ["number", "null"], "minimum": 0, "maximum": 1})
    assert out == {"anyOf": [{"type": "number"}, {"type": "null"}]}


def test_single_pedal_allows_one_swept_block_on_exp1():
    cat = engine.catalog()
    setup = _setup(wah=True, expression=True)
    assert setup.exp_pedal == "single"
    with pytest.raises(LLMGenerationError, match="ONE expression pedal"):
        engine.check_blocks([cat.get("Fassel"), cat.get("Cali Rectifire"), cat.get("4x12 Cali V30"),
                             cat.get("Volume")], setup)

    fake = FakeClaude(["Cali Rectifire", "4x12 Cali V30", "Volume", "Plate Reverb"])
    result = engine.generate("Song", _setup(expression=True), fake)
    volume = next(b for b in result.chain["blocks"] if b["model"] == "Volume")
    assert volume["expression"] == 1
    assert "ONE ordinary expression pedal" in fake.requests[0].prompt


def test_mission_pedal_splits_wah_and_volume():
    setup = _setup(wah=True, expression=True)
    setup.exp_pedal = "mission"
    fake = FakeClaude(["Fassel", "Cali Rectifire", "4x12 Cali V30", "Volume", "Plate Reverb"])
    result = engine.generate("Song", setup, fake)
    pedals = {b["model"]: b.get("expression") for b in result.chain["blocks"]}
    assert pedals["Fassel"] == 1 and pedals["Volume"] == 2


def test_input_gate_follows_the_setting_and_lands_in_the_preset():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    result = engine.generate("Song", _setup(), fake)          # default: gate on
    inp = result.preset["data"]["tone"]["dsp0"]["inputA"]
    assert inp["noiseGate"] is True and inp["threshold"] == -55.0
    assert "Input noise gate: ON" in fake.requests[1].prompt

    setup = _setup()
    setup.gate_mode = "off"
    result = engine.generate("Song", setup, FakeClaude(["Cali Rectifire", "4x12 Cali V30"]))
    assert result.preset["data"]["tone"]["dsp0"]["inputA"]["noiseGate"] is False


def test_gate_threshold_is_clamped():
    gate = engine._parse_gate({"enabled": True, "threshold_db": -200, "why": ""}, _setup())
    assert gate["threshold_db"] == -96.0


def test_a_chain_over_the_dsp_budget_is_sent_back():
    cat = engine.catalog()
    chain = [cat.get(n) for n in ("Red Squeeze", "Poly Pitch", "Pitch Wham", "Script Mod Phase",
                                  "Brit Plexi Brt", "4x12 Greenback 25", "63 Spring Reverb",
                                  xl.LOOPER_MODEL)]
    with pytest.raises(LLMGenerationError, match="Too heavy"):
        engine.check_blocks(chain, _setup(looper=True, expression=True))
    light = [cat.get(n) for n in ("Scream 808", "Brit Plexi Brt", "4x12 Greenback 25",
                                  "Simple Delay", "Glitz", xl.LOOPER_MODEL)]
    engine.check_blocks(light, _setup(looper=True))


def test_a_dsp_refusal_lowers_the_budget():
    cat = engine.catalog()
    placed = ["HD2_AmpBritPlexiBrt", "HD2_CompressorRedSqueezeMono", "HD2_LooperOneSwitchMono",
              "HD2_Reverb63SpringStereo", "HD2_PhaserScriptModPhaseMono"]
    learned = xl.learn_dsp_refusal(placed, "L6SPB_PolyDowntuneMono", cat, {})
    assert learned is not None and learned < xl.DSP_BUDGET
    assert xl.dsp_budget({"dsp_budget": learned}) == learned
    # A later, looser refusal never raises it again.
    assert xl.learn_dsp_refusal(["HD2_AmpBritPlexiBrt"], "HD2_Reverb63SpringStereo", cat,
                                {"dsp_budget": learned}) == max(xl.DSP_BUDGET_FLOOR, min(learned, 39.0))


def test_stereo_only_controls_are_not_offered_to_snapshots():
    fake = FakeClaude(["Cali Rectifire", "4x12 Cali V30", "Script Mod Phase"])
    engine.generate("Song", _setup(), fake)
    snap = next(r for r in fake.requests if r.schema_name == "snapshots")
    block3 = snap.schema["properties"]["snapshots"]["items"]["properties"]["blocks"]["properties"]["block3"]
    assert "Spread" not in block3["properties"]["parameters"]["properties"]
    assert xl.is_stereo_only("PanDry") and not xl.is_stereo_only("Mix")


def test_capo_toggle_is_enforced():
    cat = engine.catalog()
    chain = [cat.get("Cali Rectifire"), cat.get("4x12 Cali V30")]
    with pytest.raises(LLMGenerationError, match="Poly Capo toggle is ON"):
        engine.check_blocks(chain, _setup(capo=True))
    with pytest.raises(LLMGenerationError, match="Poly Capo toggle is OFF"):
        engine.check_blocks([cat.get(xl.CAPO_MODEL), *chain], _setup())
    engine.check_blocks([cat.get(xl.CAPO_MODEL), *chain, cat.get("Simple Delay")], _setup(capo=True))
    assert "Poly Capo ON" in _setup(capo=True).describe()


def test_a_heavy_capo_chain_names_what_still_fits():
    cat = engine.catalog()
    chain = [cat.get(n) for n in (xl.CAPO_MODEL, "Cali Rectifire", "4x12 Cali V30", "Glitz",
                                  "Simple Delay")]
    with pytest.raises(LLMGenerationError, match=r"Keep the Poly Capo.*Still fits"):
        engine.check_blocks(chain, _setup(capo=True))


def test_dsp_room_with_the_capo_on_a_full_rig():
    room = xl.dsp_room("full", {"capo": True}, {})
    assert room["used"] == 78 and room["left"] == 22 and room["slots_left"] == 5
    kinds = {k["kind"]: k["max"] for k in room["kinds"]}
    assert kinds["Delay"] == 2 and kinds["Reverb"] == 2 and kinds["Drive / fuzz"] == 5
    assert kinds["Premium reverb"] == 1 and kinds["Second Poly block"] == 0
    assert room["example"] == ["Drive / fuzz", "Delay", "Reverb"]
    # Whatever the example suggests passes the generator's own check.
    models = [engine.catalog().get(n) for n in (
        xl.CAPO_MODEL, "Cali Rectifire", "4x12 Cali V30", "Minotaur", "Simple Delay", "Plate Reverb")]
    assert xl.dsp_total(models) <= room["budget"]
    # A learned, lower budget shrinks the room.
    assert xl.dsp_room("full", {"capo": True}, {"dsp_budget": 85})["left"] == 7


def test_snapshots_drop_small_value_nudges_but_keep_switches_and_big_moves():
    cat = engine.catalog()
    models = [cat.get("Scream 808"), cat.get("Cali Rectifire"), cat.get("Plate Reverb")]
    blocks = [{"model": m.display_name, "parameters": {}} for m in models]
    blocks[1]["parameters"] = {"Drive": 0.7, "Mid": 0.5}
    blocks[2]["parameters"] = {"Mix": 0.15}
    drive_span = models[1].get_parameter("Drive").controller_range()
    big_drop = 0.7 - 0.4 * (drive_span[1] - drive_span[0])
    snaps = [
        {"name": "INTRO", "blocks": {"0": {"enabled": False},
                                     "1": {"parameters": {"Drive": big_drop, "Mid": 0.55}}}},
        {"name": "VERSE", "blocks": {"1": {"parameters": {"Mid": 0.45}}}},
        {"name": "CHORUS", "blocks": {"2": {"enabled": True, "parameters": {"Mix": 0.25}}}},
        {"name": "SOLO", "blocks": {"1": {"parameters": {"Mid": 0.6}}}},
    ]
    out = engine.settle_snapshots(snaps, blocks, models)
    assert out[0]["blocks"] == {"0": {"enabled": False}, "1": {"parameters": {"Drive": big_drop}}}
    assert out[1]["blocks"] == {}
    assert out[2]["blocks"] == {"2": {"enabled": True}}
    assert out[3]["blocks"] == {}
    # A section the player's feedback names keeps what Claude wrote for it.
    asked = engine.settle_snapshots(snaps, blocks, models, "the solo needs a touch more mids")
    assert asked[3]["blocks"] == {"1": {"parameters": {"Mid": 0.6}}}
    assert asked[1]["blocks"] == {}


def test_snapshot_prompt_asks_for_on_off_switching():
    fake = FakeClaude(["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"])
    engine.generate("Enter Sandman", _setup(), fake)
    snap = next(r.prompt for r in fake.requests if r.schema_name == "snapshots")
    assert "share one set of values" in snap
