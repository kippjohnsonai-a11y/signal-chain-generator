from .test_server import _wait, client  # noqa: F401 - pytest fixture


def _ids(client):  # noqa: F811
    state = client.get("/api/state").json()
    guitar = client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    return guitar, {r["kind"]: r["id"] for r in state["rigs"]}


def test_blank_song_then_build_it_by_hand(client):  # noqa: F811
    guitar, rigs = _ids(client)
    made = client.post("/api/songs/blank", json={"title": "My Song", "artist": "Me", "guitar_id": guitar,
                                                 "rig_id": rigs["full"], "tempo": 96}).json()
    v = client.get(f"/api/versions/{made['version_id']}").json()
    assert v["kind"] == "blank" and v["brief"]["origin"] == "blank"
    assert [b["model"] for b in v["view"]["blocks"]] == ["US Deluxe Nrm", "1x12 US Deluxe"]
    assert [s["name"] for s in v["view"]["snapshots"]] == ["INTRO", "VERSE", "CHORUS", "SOLO"]
    assert round(v["view"]["tempo"]) == 96
    song = client.get(f"/api/songs/{made['song_id']}").json()
    assert song["title"] == "My Song" and song["artist"] == "Me"

    empty = client.post("/api/songs/blank", json={"title": "Empty", "guitar_id": guitar,
                                                  "rig_id": rigs["full"], "start": "empty"}).json()
    res = client.post(f"/api/versions/{empty['version_id']}/edit", json={"edits": [
        {"insert": 0, "model": "Scream 808"},
        {"tempo": 140},
        {"snapshot_name": 1, "name": "Riff"},
    ]})
    assert res.status_code == 200, res.text
    v2 = client.get(f"/api/versions/{res.json()['version_id']}").json()
    assert [b["model"] for b in v2["view"]["blocks"]] == ["Scream 808"]
    assert round(v2["view"]["tempo"]) == 140
    assert v2["view"]["snapshots"][1]["name"] == "Riff"

    # Claude can write the plan later without touching the preset.
    job = client.post(f"/api/versions/{made['version_id']}/plan").json()
    _wait(client, job["job_id"])
    planned = client.get(f"/api/versions/{made['version_id']}").json()
    assert planned["brief"]["title"] == "Enter Sandman" and "origin" not in planned["brief"]
    assert planned["chain"] == v["chain"]

    assert client.post("/api/songs/blank", json={"title": " ", "guitar_id": guitar,
                                                 "rig_id": rigs["full"]}).status_code == 400


def test_hlx_round_trip(client):  # noqa: F811
    guitar, rigs = _ids(client)
    job = client.post("/api/generate", json={"song": "Enter Sandman by Metallica", "guitar_id": guitar,
                                             "rig_id": rigs["full"]}).json()
    first = _wait(client, job["job_id"])
    v1 = client.get(f"/api/versions/{first['version_id']}").json()
    amp = next(b["index"] for b in v1["view"]["blocks"] if b["model"] == "Cali Rectifire")
    reverb = next(b["index"] for b in v1["view"]["blocks"] if b["model"] == "Plate Reverb")
    edited = client.post(f"/api/versions/{first['version_id']}/edit", json={"edits": [
        {"block": amp, "parameter": "Drive", "value": 8.0, "snapshot": 3},
        {"block": reverb, "expression": 1, "parameter": "Mix", "heel": 10, "toe": 60},
        {"io": "output", "parameter": "gain", "value": -3},
    ]}).json()
    source = client.get(f"/api/versions/{edited['version_id']}").json()
    hlx = client.get(f"/api/versions/{edited['version_id']}/hlx").json()

    made = client.post("/api/import/hlx", json={"hlx": hlx, "artist": "Metallica",
                                                "guitar_id": guitar, "rig_id": rigs["full"]}).json()
    imported = client.get(f"/api/versions/{made['version_id']}").json()
    assert imported["kind"] == "import"
    assert imported["preset_name"] == source["preset_name"]
    src, got = source["chain"]["blocks"], imported["chain"]["blocks"]
    assert [b["model"] for b in got] == [b["model"] for b in src]
    for a, b in zip(src, got, strict=True):
        for name, value in a.get("parameters", {}).items():
            assert b["parameters"][name] == value, (a["model"], name)
        assert a.get("switch") == b.get("switch"), a["model"]
        assert bool(a.get("no_snapshot_bypass")) == bool(b.get("no_snapshot_bypass"))
    assert got[reverb]["expression"] == 1 and got[reverb]["expression_param"] == "Mix"
    assert got[reverb]["expression_range"] == [0.1, 0.6]
    assert imported["chain"]["output"]["gain"] == -3.0
    # What the pedal would get is the same, snapshot by snapshot.
    for a, b in zip(source["view"]["snapshots"], imported["view"]["snapshots"], strict=True):
        assert a["name"] == b["name"] and a["enabled"] == b["enabled"]
        assert a["values"] == b["values"]
    assert source["view"]["blocks"] == [{**b, "why": a["why"]} for a, b in
                                        zip(source["view"]["blocks"], imported["view"]["blocks"], strict=True)]


def test_import_rejects_a_non_preset(client):  # noqa: F811
    guitar, rigs = _ids(client)
    res = client.post("/api/import/hlx", json={"hlx": {"hello": 1}, "guitar_id": guitar, "rig_id": rigs["full"]})
    assert res.status_code == 400 and "Helix preset" in res.json()["detail"]


def test_import_notes_what_did_not_fit(client):  # noqa: F811
    guitar, rigs = _ids(client)
    hlx = {"data": {"device": 2162689, "meta": {"name": "Floor patch"}, "tone": {
        "global": {"@tempo": 100.0},
        "dsp0": {
            "block0": {"@model": "HD2_DistScream808Mono", "@position": 0, "@enabled": True, "Gain": 0.4,
                       "Mystery": 3},
            "block1": {"@model": "HD2_NotARealModel", "@position": 1, "@enabled": True},
            "block2": {"@model": "HD2_ReverbPlate", "@position": 0, "@path": 1, "@enabled": True},
        },
        "dsp1": {"block0": {"@model": "HD2_DelaySimpleDelayMono", "@position": 0}},
    }}}
    made = client.post("/api/import/hlx", json={"hlx": hlx, "guitar_id": guitar, "rig_id": rigs["full"]}).json()
    v = client.get(f"/api/versions/{made['version_id']}").json()
    assert [b["model"] for b in v["chain"]["blocks"]] == ["Scream 808", "Plate Reverb"]
    for phrase in ("another Helix", "second DSP", "parallel paths", "HD2_NotARealModel", "Mystery"):
        assert phrase in v["notes"], phrase
    assert len(v["view"]["snapshots"]) == 4


def test_stereo_variant_survives_import():
    """A stereo block read off the pedal is rebuilt stereo, not as the Mono model
    (a different model on the pedal, so live edit called the slot overwritten)."""
    from stompxl import engine, importer

    chain = {"title": "T", "meta": {"name": "T"}, "global": {"@tempo": 120},
             "blocks": [{"model": "Plate Reverb", "why": "", "parameters": {}, "stereo": True},
                        {"model": "Scream 808", "why": "", "parameters": {}}]}
    preset, _ = engine.build_preset(chain)
    dsp0 = preset["data"]["tone"]["dsp0"]
    for key in ("block0", "block1"):
        model = dsp0[key]["@model"]
        dsp0[key]["@model"] = model + ("Stereo" if dsp0[key].get("@stereo") else "Mono")
    back, _ = importer.hlx_to_chain(preset)
    blocks = {b["model"]: b for b in back["blocks"]}
    assert blocks["Plate Reverb"].get("stereo") is True
    assert "stereo" not in blocks["Scream 808"]
    rebuilt, _ = engine.build_preset(back)
    flags = {b["@model"]: b["@stereo"] for k, b in rebuilt["data"]["tone"]["dsp0"].items()
             if k.startswith("block")}
    assert flags[dsp0["block0"]["@model"].removesuffix("Stereo")] is True
