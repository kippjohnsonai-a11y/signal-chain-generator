"""Reading a preset off the pedal: device document -> .hlx shape -> chain."""

import json

from test_device_document import _block, _document, _footswitch, _preset, _snapshot

from stompxl.helixgen.device import document as d
from stompxl.helixgen.device.symbols import DeviceSymbols
from stompxl import engine, importer, pedal, pedal_import

from .test_server import _wait, client  # noqa: F401 - pytest fixture

SCREAM = ["Gain", "Tone", "Level"]
AMP = ["Drive", "Bass", "Mid", "Treble", "ChVol", "Master", "Presence", "Ripple", "Sag", "Hum",
       "Bias", "BiasX"]
CAB = ["Mic", "Distance", "LowCut", "HighCut", "Level", "Angle", "Position"]
PLATE = ["Decay", "Predelay", "LowCut", "HighCut", "Mix", "Level"]

SYMBOLS = DeviceSymbols.parse(json.dumps([
    {"symbol": "HD2_DistScream808Mono", "parameters": SCREAM},          # 0
    {"symbol": "HD2_AmpCaliRectifire", "parameters": AMP},              # 1
    {"symbol": "HD2_CabMicIr_4x12CaliV30", "parameters": CAB},          # 2
    {"symbol": "HD2_ReverbPlateStereo", "parameters": PLATE},           # 3
    {"symbol": "HelixStomp_AppDSPFlowInput", "parameters": ["noiseGate", "threshold", "decay"]},
    {"symbol": "HelixStomp_AppDSPFlowOutputMain", "parameters": ["pan", "gain"]},
    {"symbol": "HD2_AppDSPFlowJoin",
     "parameters": ["A Level", "A Pan", "B Level", "B Pan", "B Polarity", "Level"]},
]))


def _assignment(ident, source, slot, param, low=0.0, high=1.0):
    return {0: ident, 1: {0: source, 1: 4, 2: low, 3: high, 4: 0, 5: slot,
                          6: {28: 0, 29: param, 41: False}, 7: 0, 13: False}}


def _pedal_document():
    preset = _preset()
    array = preset[0][d.KEY_SLOT_ARRAY]
    array[0] = {d.KEY_TAG: 0, d.KEY_CONTENT: {6: 1, 7: {2: 3, 3: 3, 4: [True, -52.0, 0.3]}}}
    array[1] = _block(0, [0.7, 0.5, 0.6])                                 # Scream 808
    amp = _block(1, [0.6, 0.4, 0.5, 0.5, 0.7, 0.3, 0.5, 0.5, 0.5, 0.2, 0.5, 0.5])
    amp[d.KEY_CONTENT][d.KEY_MODEL_REF][d.KEY_CAB_INDEX] = 2              # its paired cab
    amp[d.KEY_CONTENT][d.KEY_CAB_VALUES] = {2: 7, 3: 7, 4: [3, 2.0, 80.0, 8000.0, 0.0, 0.0, 0.3]}
    array[2] = amp
    plate = _block(3, [0.5, 0.02, 100.0, 8000.0, 0.25, 0.0])
    plate[d.KEY_CONTENT][d.KEY_ENABLED] = False
    array[3] = plate
    array[9] = {d.KEY_TAG: 1, d.KEY_CONTENT: {6: 1, 7: {2: 2, 3: 2, 4: [0.5, -2.0]}}}
    for i in range(4, 9):
        array[i] = {d.KEY_TAG: d.TAG_EMPTY, d.KEY_CONTENT: None}
    array[19] = {d.KEY_TAG: 3, d.KEY_CONTENT: {17: {7: {2: 6, 3: 6, 4: [0.0, 0.5, 0.0, 0.5, False, 0.0]}}}}

    snaps = [_snapshot(n) for n in ("Verse", "Chorus", "Bridge", "Solo")]
    for i, snap in enumerate(snaps):
        snap[3][3] = [None, i == 3]                       # the plate is on only in Solo
        snap[2][1] = [False, 1, [0.6, 0.6, 0.6, 0.85][i]]  # amp Drive per snapshot (id 1)
        snap[2][0] = [False, 0, 0.0]
        snap[2][2] = [False, 2, 0.6]                       # Scream Level: same everywhere (id 2)
        snap[5] = 120.0 if i == 0 else 132.0               # Verse at 120, the rest at 132
    preset[10][10] = snaps
    preset[10][d.KEY_CURRENT_SNAPSHOT] = 0
    preset[4] = ([None, [_assignment(0, 1, 3, 4, 0.1, 0.6)]]   # EXP 1 -> plate Mix, 10-60%
                 + [None] * 10                                  # Snapshots -> amp Drive, Scream Level
                 + [[_assignment(1, 12, 2, 0), _assignment(2, 12, 1, 2)]])
    preset[3][8] = [None, None, None, None, _footswitch(1, "SCREAM"), None, None, None]
    doc = d.parse(_document())
    doc.preset = preset
    return doc


def test_a_pedal_preset_becomes_a_chain():
    hlx = pedal_import.document_to_hlx(_pedal_document(), SYMBOLS, engine.catalog(), "Sandman")
    chain, notes = importer.hlx_to_chain(hlx)
    blocks = chain["blocks"]
    assert [b["model"] for b in blocks] == ["Scream 808", "Cali Rectifire", "4x12 Cali V30", "Plate Reverb"]
    assert blocks[0]["parameters"]["Gain"] == 0.7
    assert blocks[0]["switch"] == 5 and blocks[0]["footswitch_label"] == "SCREAM"
    assert blocks[2]["parameters"]["Distance"] == 2.0       # the paired cab's own values
    assert blocks[3].get("enabled") is False
    assert blocks[3]["expression"] == 1 and blocks[3]["expression_param"] == "Mix"
    assert blocks[3]["expression_range"] == [0.1, 0.6]
    names = [s["name"] for s in chain["snapshots"]]
    assert names == ["Verse", "Chorus", "Bridge", "Solo"]
    assert chain["snapshots"][3]["blocks"]["3"]["enabled"] is True
    assert chain["snapshots"][3]["blocks"]["1"]["parameters"]["Drive"] == 0.85
    assert chain["input"] == {"noiseGate": True, "threshold": -52.0, "decay": 0.3}
    assert chain["output"] == {"gain": -2.0, "pan": 0.5}
    assert chain["meta"]["name"] == "Sandman" and notes == []
    # Snapshot control is kept even where every snapshot holds the same value.
    assert all(s["blocks"]["0"]["parameters"]["Level"] == 0.6 for s in chain["snapshots"])
    # Each snapshot keeps its own tempo; the preset's is the current snapshot's.
    assert chain["global"]["@tempo"] == 120.0
    assert [s.get("tempo") for s in chain["snapshots"]] == [None, 132.0, 132.0, 132.0]
    assert chain["join"]["Level"] == 0.0
    # Undecoded preset-level settings are carried along to be written back.
    assert chain["device_settings"]["5"]["3"] == 3
    # Values equal to the catalog default are kept (a paired cab's defaults differ).
    assert blocks[2]["parameters"]["Level"] == 0.0
    preset, _ = engine.build_preset(chain)
    tone = preset["data"]["tone"]
    assert [tone[f"snapshot{i}"]["@tempo"] for i in range(4)] == [120.0, 132.0, 132.0, 132.0]
    assert tone["dsp0"]["join"]["Level"] == 0.0
    assert tone["controller"]["dsp0"]["block0"]["Level"]["@controller"] == 12  # the XL's Snapshots


def test_import_from_the_pedal_endpoint(client, monkeypatch):  # noqa: F811
    hlx = pedal_import.document_to_hlx(_pedal_document(), SYMBOLS, engine.catalog(), "Sandman")
    monkeypatch.setattr(pedal, "read_preset", lambda slot: hlx)
    state = client.get("/api/state").json()
    guitar = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    job = client.post("/api/import/pedal", json={"slot": 100, "artist": "Metallica", "guitar_id": guitar,
                                                 "rig_id": state["rigs"][0]["id"]}).json()
    made = _wait(client, job["job_id"])
    v = client.get(f"/api/versions/{made['version_id']}").json()
    assert v["kind"] == "import" and "pedal slot 26A" in v["notes"]
    assert client.get(f"/api/songs/{made['song_id']}").json()["title"] == "Sandman"
    assert v["preset_name"] == "Sandman"
    # A title given at import renames the preset on the pedal too.
    job = client.post("/api/import/pedal", json={"slot": 100, "title": "Joker live take two!",
                                                 "guitar_id": guitar, "rig_id": state["rigs"][0]["id"]}).json()
    renamed = client.get(f"/api/versions/{_wait(client, job['job_id'])['version_id']}").json()
    assert renamed["preset_name"] == "Joker live take "
    assert 'originally named "Sandman"' in renamed["notes"]
    assert client.post("/api/import/pedal", json={"slot": 999, "guitar_id": guitar,
                                                  "rig_id": state["rigs"][0]["id"]}).status_code == 400
