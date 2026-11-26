import json

from stompxl.helixgen.device.symbols import DeviceSymbols
from stompxl import audit, engine, view


def test_simple_pitch_has_hx_edit_parameters():
    model = engine.catalog().get("Simple Pitch")
    labels = {view.label(n, model.get_parameter(n)) for n in model.parameter_names()
              if not n.startswith("@")}
    assert {"Interval", "Cents", "Delay", "Shift Level", "Mix", "Level"} <= labels
    interval = model.get_parameter("Interval1")
    assert interval.normalize(12) == 12
    assert view.editor("Interval1", interval) == {"kind": "number", "min": -24, "max": 24, "step": 1}


def test_audit_lists_gaps_both_ways():
    symbols = DeviceSymbols.parse(json.dumps([
        {"symbol": "HD2_PitchSimplePitchMono",
         "parameters": ["Interval1", "Cents1", "Time1", "LevelVoice1", "Mix", "Level", "Formant"]},
        {"symbol": "HD2_PitchSimplePitchStereo",
         "parameters": ["Interval1", "Cents1", "Time1", "LevelVoice1", "Mix", "Level",
                        "PanVoice1", "PanDry"]},
    ]))
    gaps = audit.audit(symbols)
    assert gaps == {"Simple Pitch": {
        "symbol": "HD2_PitchSimplePitchMono/HD2_PitchSimplePitchStereo",
        "missing": ["Formant"], "unknown": []}}


def test_added_pitch_parameters_have_no_default():
    """Unset, they aren't written, so the pedal keeps the model's own value."""
    from stompxl.helixgen.generator import generate_preset

    cat = engine.catalog()
    wham = cat.get("Pitch Wham")
    assert {"Heel", "Toe"} <= set(wham.parameter_names())
    assert wham.get_parameter("Heel").default_value() is None
    assert cat.get("Simple Pitch").get_parameter("Interval1").default_value() is None
    assert "Lock" not in cat.get("Glitch Delay").parameter_names()

    from stompxl import xl
    preset, _ = generate_preset({"blocks": [{"model": "Simple Pitch", "parameters": {}}]},
                                cat, xl.template())
    assert "Interval1" not in preset["data"]["tone"]["dsp0"]["block0"]


def test_audit_skips_internal_parameters():
    symbols = DeviceSymbols.parse(json.dumps([
        {"symbol": "HD2_CabMicIr_4x12CaliV30",
         "parameters": [*(n for n in engine.catalog().get("4x12 Cali V30").parameter_names()
                          if not n.startswith("@")), "IrData"]},
    ]))
    assert audit.audit(symbols) == {}
