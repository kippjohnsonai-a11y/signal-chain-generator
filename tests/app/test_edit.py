import pytest

from stompxl import xl

from .test_server import _wait, client  # noqa: F401 - pytest fixture


def _first_version(client):  # noqa: F811
    state = client.get("/api/state").json()
    strat = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    job = client.post("/api/generate", json={"song": "Enter Sandman by Metallica",
                                             "guitar_id": strat,
                                             "rig_id": state["rigs"][0]["id"]}).json()
    return _wait(client, job["job_id"])


def _param(view, block, name):
    return next(p for p in view["blocks"][block]["params"] if p["name"] == name)


def test_hand_edit_saves_a_new_version(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    reverb = next(b for b in v1["view"]["blocks"] if b["model"] == "Plate Reverb")
    mix = _param(v1["view"], reverb["index"], "Mix")
    assert mix["edit"]["kind"] == "number" and mix["edit"]["max"] == 100
    amp = next(b for b in v1["view"]["blocks"] if b["model"] == "Cali Rectifire")

    res = client.post(f"/api/versions/{first['version_id']}/edit", json={"edits": [
        {"block": reverb["index"], "parameter": "Mix", "value": 42},
        {"block": amp["index"], "parameter": "Drive", "value": 6.5, "snapshot": 1},
        {"block": reverb["index"], "enabled": False, "snapshot": 0},
    ]})
    assert res.status_code == 200, res.text
    v2 = client.get(f"/api/versions/{res.json()['version_id']}").json()
    assert v2["kind"] == "manual" and v2["parent_id"] == first["version_id"]
    assert "Plate Reverb Mix" in v2["notes"]
    assert _param(v2["view"], reverb["index"], "Mix")["value"] == "42.0"
    snap1 = {p["name"]: p["value"] for p in v2["view"]["snapshots"][1]["values"][amp["key"]]}
    assert snap1["Drive"] == "6.5"
    assert v2["view"]["snapshots"][0]["enabled"][reverb["key"]] is False
    # The parent is untouched.
    v1_again = client.get(f"/api/versions/{first['version_id']}").json()
    assert _param(v1_again["view"], reverb["index"], "Mix")["value"] == mix["value"]


def test_rehearsal_save_is_named_and_starred(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    amp = next(b for b in v1["view"]["blocks"] if b["model"] == "Cali Rectifire")
    edit = {"edits": [{"block": amp["index"], "parameter": "Drive", "value": 4}], "rehearsal": True}
    one = client.post(f"/api/versions/{first['version_id']}/edit", json=edit).json()
    two = client.post(f"/api/versions/{one['version_id']}/edit", json=edit).json()
    labels = {v["id"]: v["label"] for v in client.get(f"/api/songs/{first['song_id']}").json()["versions"]}
    assert labels[one["version_id"]].startswith("Rehearsal ")
    assert labels[two["version_id"]] == labels[one["version_id"]] + " (2)"
    assert client.get(f"/api/songs/{first['song_id']}").json()["favorite_version_id"] == two["version_id"]
    # A plain hand edit keeps its old name and leaves the star alone.
    three = client.post(f"/api/versions/{two['version_id']}/edit", json={"edits": edit["edits"]}).json()
    song = client.get(f"/api/songs/{first['song_id']}").json()
    assert song["favorite_version_id"] == two["version_id"]
    assert next(v for v in song["versions"] if v["id"] == three["version_id"])["label"].startswith("Hand edit")


def test_hand_edit_rejects_bad_values(client):  # noqa: F811
    first = _first_version(client)
    url = f"/api/versions/{first['version_id']}/edit"
    assert client.post(url, json={"edits": []}).status_code == 400
    assert client.post(url, json={"edits": [{"block": 99, "parameter": "Mix", "value": 1}]}).status_code == 400
    assert client.post(url, json={"edits": [{"block": 0, "parameter": "Nope", "value": 1}]}).status_code == 400
    assert client.post(url, json={"edits": [{"block": 1, "parameter": "Drive", "value": "loud"}]}).status_code == 400


def _post(client, version_id, edits):  # noqa: F811
    res = client.post(f"/api/versions/{version_id}/edit", json={"edits": edits})
    assert res.status_code == 200, res.text
    return client.get(f"/api/versions/{res.json()['version_id']}").json()


def test_swap_add_remove_blocks(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    models = [b["model"] for b in v1["view"]["blocks"]]
    reverb = models.index("Plate Reverb")

    # Preview shows the change without saving anything.
    preview = client.post(f"/api/versions/{first['version_id']}/preview",
                          json={"edits": [{"block": reverb, "model": "Dynamic Hall"}]}).json()
    assert preview["view"]["blocks"][reverb]["model"] == "Dynamic Hall"
    assert len(client.get(f"/api/songs/{first['song_id']}").json()["versions"]) == 1

    v2 = _post(client, first["version_id"], [
        {"block": reverb, "model": "Dynamic Hall"},
        {"insert": 0, "model": "Chrome Custom"},  # a wah, first in the chain
        {"remove": 1},  # what was block 0 before the insert
    ])
    after = [b["model"] for b in v2["view"]["blocks"]]
    assert after[0] == "Chrome Custom" and "Dynamic Hall" in after and models[0] not in after[:2]
    assert v2["chain"]["blocks"][0]["expression"] == 1  # the wah got the pedal
    assert "Plate Reverb -> Dynamic Hall" in v2["notes"]


def test_footswitch_and_expression(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    blocks = v1["chain"]["blocks"]
    amp = next(i for i, b in enumerate(blocks) if b["model"] == "Cali Rectifire")
    taken = next(i for i, b in enumerate(blocks) if b.get("switch"))
    switch = blocks[taken]["switch"]
    delay_or_reverb = next(i for i, b in enumerate(blocks) if b["model"] == "Plate Reverb")

    v2 = _post(client, first["version_id"], [
        {"block": amp, "switch": switch},
        {"block": delay_or_reverb, "expression": 1, "parameter": "Mix", "heel": 10, "toe": 60},
    ])
    chain = v2["chain"]["blocks"]
    assert chain[amp]["switch"] == switch and "switch" not in chain[taken]
    assert chain[delay_or_reverb]["expression_param"] == "Mix"
    assert chain[delay_or_reverb]["expression_range"] == [0.1, 0.6]
    mix = next(p for p in v2["view"]["blocks"][delay_or_reverb]["params"] if p["param"] == "Mix")
    assert mix["controlled_by"] == "EXP 1"
    assert "taken from" in v2["notes"]


def test_block_edits_reject_bad_input(client):  # noqa: F811
    first = _first_version(client)
    url = f"/api/versions/{first['version_id']}/edit"
    for bad in ({"block": 0, "model": "No Such Pedal"}, {"remove": 99},
                {"block": 0, "switch": 9}, {"block": 1, "expression": 1}):
        assert client.post(url, json={"edits": [bad]}).status_code == 400, bad


def test_explain_a_block_and_cache_it(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    reverb = next(b["index"] for b in v1["view"]["blocks"] if b["model"] == "Plate Reverb")
    url = f"/api/versions/{first['version_id']}/explain"

    job = client.post(url, json={"block": reverb}).json()
    result = _wait(client, job["job_id"])
    assert result["explanation"]["parameters"][0]["name"] == "Mix"
    assert result["explanation"]["model"] == "Plate Reverb"
    # Asked again, it comes straight from the cache.
    assert client.post(url, json={"block": reverb}).json()["explanation"]["summary"] == "A plate reverb."
    # A block swapped in edit mode is explained as the new model.
    job = client.post(url, json={"block": reverb, "edits": [{"block": reverb, "model": "Dynamic Hall"}]}).json()
    assert _wait(client, job["job_id"])["explanation"]["model"] == "Dynamic Hall"
    assert client.post(url, json={"block": 99}).status_code == 400


def test_edit_input_gate_and_output(client):  # noqa: F811
    first = _first_version(client)
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    io = {b["io"]: {p["param"]: p for p in b["params"]} for b in v1["view"]["io"]}
    assert io["input"]["noiseGate"]["edit"]["kind"] == "toggle"
    assert io["output"]["gain"]["edit"]["max"] == 6

    v2 = _post(client, first["version_id"], [
        {"io": "input", "parameter": "noiseGate", "value": True},
        {"io": "input", "parameter": "threshold", "value": -40},
        {"io": "input", "parameter": "decay", "value": 30},
        {"io": "output", "parameter": "gain", "value": -3},
        {"io": "output", "parameter": "pan", "value": 50},
    ])
    tone_in = v2["chain"]["input"]
    assert tone_in == {"noiseGate": True, "threshold": -40.0, "decay": 0.3}
    assert v2["chain"]["gate"]["threshold_db"] == -40.0
    assert v2["chain"]["output"] == {"gain": -3.0, "pan": 0.5}
    shown = {b["io"]: {p["param"]: p["value"] for p in b["params"]} for b in v2["view"]["io"]}
    assert shown["input"] == {"noiseGate": "On", "threshold": "-40", "decay": "30.0"}
    assert shown["output"]["gain"] == "-3.0"
    assert "Output Level (dB): 0.0 -> -3.0" in v2["notes"]
    # The .hlx carries them.
    hlx = client.get(f"/api/versions/{v2['id']}/hlx").json()["data"]["tone"]["dsp0"]
    assert hlx["outputA"]["gain"] == -3.0 and hlx["inputA"]["threshold"] == -40.0

    job = client.post(f"/api/versions/{v2['id']}/explain", json={"io": "output"}).json()
    assert _wait(client, job["job_id"])["explanation"]["model"] == "HX Stomp Output Main"
    bad = client.post(f"/api/versions/{v2['id']}/edit",
                      json={"edits": [{"io": "output", "parameter": "nope", "value": 1}]})
    assert bad.status_code == 400


def test_snapshot_edits_after_adding_a_block_stick():
    """Adding a block rebuilds the snapshot list; later edits must land in the new one."""
    from stompxl import edit, importer

    chain = importer.blank_chain("x", "full", "amp", "on", 120)
    out, _ = edit.apply_edits(chain, None, [
        {"insert": 0, "model": "Scream 808"},
        {"block": 1, "parameter": "Drive", "value": 7, "snapshot": 2},
        {"snapshot_name": 3, "name": "Lead"},
    ])
    assert out["snapshots"][2]["blocks"]["1"]["parameters"]["Drive"] == 0.7
    assert out["snapshots"][3]["name"] == "Lead"


def test_move_a_block_and_its_snapshot_changes_follow():
    from stompxl import edit, importer

    chain = importer.blank_chain("x", "full", "amp", "on", 120)
    chain, _ = edit.apply_edits(chain, None, [
        {"insert": 0, "model": "Scream 808"},
        {"insert": 1, "model": "Parametric"},
        {"block": 1, "enabled": False, "snapshot": 2},
    ])
    last = len(chain["blocks"]) - 1
    out, changes = edit.apply_edits(chain, None, [{"move": 1, "to": last}])
    moved = [b["model"] for b in out["blocks"]]
    assert moved[last] == "Parametric" and moved[0] == "Scream 808"
    assert moved[1:last] == [b["model"] for b in chain["blocks"]][2:]
    assert changes == [f"Moved Parametric to block {last + 1}"]
    assert out["snapshots"][2]["blocks"][str(last)]["enabled"] is False
    with pytest.raises(edit.EditError, match="Can't move"):
        edit.apply_edits(chain, None, [{"move": 0, "to": 99}])


def test_moving_a_block_across_the_computer_nam_changes_its_path():
    from stompxl import edit, engine

    chain = {"blocks": [
        {"model": xl.RETURN_MODEL, "parameters": {}},
        {"model": "Scream 808", "parameters": {}},
        {"model": "Parametric", "parameters": {}, "amp_side": "before"},
        {"model": "Simple Delay", "parameters": {}},
    ], "snapshots": [], "nam": {"mode": "usb", "capture": {"name": "x"}}}
    cat = engine.catalog()
    out, _ = edit.apply_edits(chain, None, [{"move": 2, "to": 3}])
    assert [b["model"] for b in out["blocks"]][2:] == ["Simple Delay", "Parametric"]
    assert xl.nam_split(out["blocks"], [cat.get(b["model"]) for b in out["blocks"]]) == 2
    back, _ = edit.apply_edits(out, None, [{"move": 3, "to": 1}])
    assert back["blocks"][1]["model"] == "Parametric" and back["blocks"][1]["amp_side"] == "before"
    with pytest.raises(edit.EditError, match="stays first"):
        edit.apply_edits(chain, None, [{"move": 1, "to": 0}])
