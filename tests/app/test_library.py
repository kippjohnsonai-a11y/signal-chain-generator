"""Folders, tags (the player's and the automatic NAM / Poly Capo ones) and
deleting several versions at once."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from stompxl import config, xl
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude


def _add(lib, song_id, blocks=(), rig_kind="full", parent_id=None, nam=None):
    rig = next(r for r in lib.rigs() if r["kind"] == rig_kind)
    chain = {"blocks": [{"model": m} for m in blocks]}
    if nam:
        chain["nam"] = nam
    return lib.add_version(song_id=song_id, parent_id=parent_id, kind="blank", label="",
                           guitar_id=None, rig_id=rig["id"], options={}, brief={}, chain=chain,
                           preset={})


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "lib.sqlite3")


def _tags(lib, version_id):
    return [(t["tag"], t["auto"]) for t in lib.version(version_id)["tags"]]


def test_auto_tags_for_nam_and_poly_capo(lib):
    song = lib.add_song("x", "Song", "")
    plain = _add(lib, song, ["Plate Reverb"])
    capo = _add(lib, song, [xl.CAPO_MODEL, "Plate Reverb"])
    pedal = _add(lib, song, [xl.NAM_MODEL], rig_kind="nam_pedal", nam={"mode": "pedal"})
    usb = _add(lib, song, [xl.CAPO_MODEL], rig_kind="nam_usb", nam={"mode": "usb"})
    assert _tags(lib, plain) == []
    assert _tags(lib, capo) == [("Poly Capo", True)]
    assert _tags(lib, pedal) == [("NAM", True)]
    assert _tags(lib, usb) == [("NAM", True), ("Poly Capo", True)]
    # A Xenomorph Fuzz on an ordinary rig is just a fuzz.
    assert _tags(lib, _add(lib, song, [xl.NAM_MODEL])) == []
    assert {t["tag"]: t["count"] for t in lib.tags()} == {"NAM": 2, "Poly Capo": 2}


def test_player_tags_follow_tweaks_but_auto_tags_are_reworked(lib):
    song = lib.add_song("x", "Song", "")
    first = _add(lib, song, [xl.CAPO_MODEL])
    lib.set_tags(first, ["live", " Live ", "church  set"])
    assert _tags(lib, first) == [("Poly Capo", True), ("church set", False), ("live", False)]
    # The tweak drops the capo: the capo tag goes, the player's stay.
    tweak = _add(lib, song, ["Plate Reverb"], parent_id=first)
    assert _tags(lib, tweak) == [("church set", False), ("live", False)]
    # A new tag takes the library's spelling, and "nam" by hand is the NAM tag.
    lib.set_tags(tweak, ["LIVE", "nam"])
    assert _tags(lib, tweak) == [("live", False), ("NAM", False)]
    # Removing every hand tag leaves the automatic ones.
    lib.set_tags(first, [])
    assert _tags(lib, first) == [("Poly Capo", True)]


def test_bulk_tag_and_delete(lib):
    song = lib.add_song("x", "Song", "")
    a, b, c = (_add(lib, song) for _ in range(3))
    lib.set_favorite(song, b)
    lib.tag_versions([a, b], add=["keep"])
    lib.tag_versions([b], remove=["KEEP"])
    assert _tags(lib, a) == [("keep", False)] and _tags(lib, b) == []
    assert lib.delete_versions([b, c, 9999]) == 2
    assert [v["id"] for v in lib.versions(song)] == [a]
    assert lib.song(song)["favorite_version_id"] is None


def test_folders(lib):
    song = lib.add_song("x", "Song", "")
    folder = lib.add_folder("Church")
    lib.set_song_folder(song, folder)
    lib.rename_folder(folder, "Sunday")
    assert lib.folders()[0]["name"] == "Sunday"
    assert lib.songs()[0]["folder_id"] == folder
    lib.delete_folder(folder)
    assert lib.folders() == [] and lib.songs()[0]["folder_id"] is None


def test_older_library_gets_folders_and_its_versions_tagged(tmp_path):
    path = tmp_path / "old.sqlite3"
    Library(path)
    conn = sqlite3.connect(path)
    # Undo the upgrade, as a library from before folders and tags.
    conn.executescript(
        "DROP TABLE version_tags; DROP TABLE folders; ALTER TABLE songs DROP COLUMN folder_id;"
        "PRAGMA user_version = 0;"
        "INSERT INTO songs (query, title, artist, created_at) VALUES ('q', 'Old', '', 0);"
        "INSERT INTO versions (song_id, kind, rig_id, options, brief, chain, preset, created_at)"
        " VALUES (1, 'generate', 1, '{}', '{}', '{\"blocks\": [{\"model\": \"Poly Capo\"}]}',"
        " '{}', 0);")
    conn.commit()
    conn.close()
    lib = Library(path)
    assert _tags(lib, 1) == [("Poly Capo", True)]
    assert lib.songs()[0]["folder_id"] is None and lib.songs()[0]["tags"] == ["Poly Capo"]


def test_api(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    lib = Library(tmp_path / "lib.sqlite3")
    client = TestClient(create_app(lib, caller_factory=lambda: FakeClaude([])))
    song = lib.add_song("x", "Song", "")
    a, b, c = _add(lib, song, [xl.CAPO_MODEL]), _add(lib, song), _add(lib, song)

    folder = client.post("/api/folders", json={"name": "  Covers "}).json()["id"]
    assert client.post("/api/folders", json={"name": " "}).status_code == 400
    assert client.put(f"/api/songs/{song}/folder", json={"folder_id": 999}).status_code == 404
    client.put(f"/api/songs/{song}/folder", json={"folder_id": folder})
    state = client.get("/api/state").json()
    assert state["folders"][0]["name"] == "Covers" and state["songs"][0]["folder_id"] == folder
    assert state["tags"] == [{"tag": "Poly Capo", "auto": 1, "count": 1}]

    tags = client.put(f"/api/versions/{a}/tags", json={"tags": ["worship"]}).json()["tags"]
    assert [t["tag"] for t in tags] == ["Poly Capo", "worship"]
    client.post("/api/versions/bulk-tags", json={"ids": [b, c], "add": ["worship"]})
    song_view = client.get(f"/api/songs/{song}").json()
    assert [[t["tag"] for t in v["tags"]] for v in song_view["versions"]] == [
        ["Poly Capo", "worship"], ["worship"], ["worship"]]
    assert client.get("/api/state").json()["songs"][0]["tag_sets"] == [
        ["Poly Capo", "worship"], ["worship"], ["worship"]]

    assert client.post("/api/versions/bulk-delete", json={"ids": [b, c]}).json() == {"deleted": 2}
    assert [v["id"] for v in client.get(f"/api/songs/{song}").json()["versions"]] == [a]
    client.delete(f"/api/folders/{folder}")
    assert client.get("/api/state").json()["songs"][0]["folder_id"] is None


def test_set_list_order_stars_and_recent(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    lib = Library(tmp_path / "lib.sqlite3")
    client = TestClient(create_app(lib, caller_factory=lambda: FakeClaude([])))
    a, b, c = (lib.add_song("x", t, "") for t in ("A", "B", "C"))
    folder = client.post("/api/folders", json={"name": "Set 1"}).json()["id"]
    for song in (a, b, c):
        client.put(f"/api/songs/{song}/folder", json={"folder_id": folder})
    positions = lambda: {s["id"]: s["set_position"] for s in client.get("/api/state").json()["songs"]}  # noqa: E731
    # Songs join the end of the set list.
    assert positions() == {a: 1, b: 2, c: 3}
    client.put(f"/api/folders/{folder}/order", json={"song_ids": [c, a]})
    assert positions() == {c: 1, a: 2, b: 3}
    # Moving a song out and back puts it last; staying put keeps its place.
    client.put(f"/api/songs/{a}/folder", json={"folder_id": folder})
    assert positions()[a] == 2
    client.put(f"/api/songs/{c}/folder", json={"folder_id": None})
    assert positions()[c] is None
    assert client.put("/api/folders/999/order", json={"song_ids": []}).status_code == 404

    client.put(f"/api/songs/{b}/star", json={"starred": True})
    client.post(f"/api/songs/{a}/opened")
    songs = {s["id"]: s for s in client.get("/api/state").json()["songs"]}
    assert songs[b]["starred"] == 1 and songs[a]["starred"] == 0
    assert songs[a]["opened_at"] and songs[b]["opened_at"] is None
    assert client.post("/api/songs/999/opened").status_code == 404


def test_sets_hold_songs_in_play_order_and_carry_over_old_folders(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    path = tmp_path / "lib.sqlite3"
    lib = Library(path)
    a, b, c = (lib.add_song("x", t, "") for t in ("A", "B", "C"))
    folder = lib.add_folder("Friday")
    for song in (b, a):
        lib.set_song_folder(song, folder)
    # A library from before sets: its folders' songs become the set, in set-list order.
    with sqlite3.connect(path) as conn:
        conn.executescript("DELETE FROM set_songs; PRAGMA user_version = 2;")
    lib = Library(path)
    assert lib.set_songs() == {folder: [b, a]}

    client = TestClient(create_app(lib, caller_factory=lambda: FakeClaude([])))
    other = client.post("/api/folders", json={"name": "Sunday"}).json()["id"]
    # One song, several sets; a newly added set gets it at the end.
    client.put(f"/api/songs/{c}/sets", json={"set_ids": [folder, other]})
    assert client.get("/api/state").json()["set_songs"] == {str(folder): [b, a, c], str(other): [c]}
    client.put(f"/api/songs/{c}/sets", json={"set_ids": [other]})
    client.put(f"/api/sets/{folder}/songs", json={"song_ids": [a, b, 999, a]})
    assert lib.set_songs() == {folder: [a, b], other: [c]}
    assert client.put(f"/api/songs/{c}/sets", json={"set_ids": [999]}).status_code == 404
    assert client.put("/api/sets/999/songs", json={"song_ids": []}).status_code == 404
    client.delete(f"/api/folders/{other}")
    assert lib.set_songs() == {folder: [a, b]}


def test_a_set_goes_to_the_pedal_in_consecutive_slots(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    from stompxl import pedal
    sent = []
    monkeypatch.setattr(pedal, "push", lambda preset, slot, device_settings=None: sent.append(slot) or "ok")
    lib = Library(tmp_path / "lib.sqlite3")
    client = TestClient(create_app(lib, caller_factory=lambda: FakeClaude([])))
    a, b = lib.add_song("x", "A", ""), lib.add_song("x", "B", "")
    va, vb = _add(lib, a), _add(lib, b)
    s = lib.add_folder("Gig")
    lib.replace_set(s, [b, a])
    assert client.post(f"/api/sets/{s}/push", json={"start_slot": xl.SLOT_COUNT - 1}).status_code == 400
    job = client.post(f"/api/sets/{s}/push", json={"start_slot": 4}).json()["job_id"]
    for _ in range(200):
        if client.get(f"/api/jobs/{job}").json()["status"] != "running":
            break
        time.sleep(0.02)
    assert client.get(f"/api/jobs/{job}").json()["status"] == "done"
    assert sent == [4, 5]
    assert lib.version(vb)["pushed_slot"] == 4 and lib.version(va)["pushed_slot"] == 5
    songs = {x["id"]: x for x in client.get("/api/state").json()["songs"]}
    assert songs[b]["pushed_slot"] == 4 and songs[b]["open_version_id"] == vb
