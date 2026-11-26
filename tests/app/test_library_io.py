"""Export a library to one zip, import it on another computer."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from stompxl import config, library_io, namlib
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude


def _app(data_dir, monkeypatch):
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(config.DATA_DIR_ENV, str(data_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    lib = Library(data_dir / "library.sqlite3")
    return lib, TestClient(create_app(lib, caller_factory=lambda: FakeClaude([])))


def test_round_trip_moves_songs_files_and_settings_but_not_keys(tmp_path, monkeypatch):
    old, client = _app(tmp_path / "old", monkeypatch)
    client.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"})
    old.add_song("Enter Sandman", "Enter Sandman", "Metallica")
    nam = tmp_path / "old" / "captures"
    config.write_settings({"anthropic_api_key": "sk-ant-old", "tone3000_key": "t3k_pub_old",
                           "claude_effort": "high", "nam_folder": str(nam),
                           "setup_done": True})
    namlib.nam_dir().mkdir(parents=True, exist_ok=True)
    (namlib.nam_dir() / "plexi.nam").write_bytes(b"capture")
    assert namlib.nam_dir() == nam / "nam"
    (config.backups_dir() / "slot-001.hlx").write_text("{}")

    res = client.get("/api/library/export")
    assert res.status_code == 200
    assert "signal-chain-library-" in res.headers["content-disposition"]
    blob = res.content
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        settings = json.loads(z.read("settings.json"))
        manifest = json.loads(z.read("manifest.json"))
    assert {"manifest.json", "library.sqlite3", "settings.json",
            "NAM/nam/plexi.nam", "backups/slot-001.hlx"} <= names
    assert settings == {"claude_effort": "high"}
    assert manifest["songs"] == 1

    new, client = _app(tmp_path / "new", monkeypatch)
    config.write_settings({"anthropic_api_key": "sk-ant-new"})
    new.add_song("Something else", "Something else", "")
    result = client.post("/api/library/import", content=blob).json()
    assert result["songs"] == 1 and result["files"] == 2
    assert [s["title"] for s in new.songs()] == ["Enter Sandman"]
    assert [g["name"] for g in new.guitars()] == ["Strat"]
    assert config.get_setting("anthropic_api_key") == "sk-ant-new"
    assert config.get_setting("tone3000_key") is None
    assert config.get_setting("claude_effort") == "high"
    assert config.get_setting("nam_folder") is None
    assert (namlib.nam_dir() / "plexi.nam").read_bytes() == b"capture"
    # The library it replaced was saved first, and can be imported back.
    before = tmp_path / "new" / result["saved_before"].rsplit("/", 1)[-1]
    with zipfile.ZipFile(before) as z:
        assert json.loads(z.read("manifest.json"))["songs"] == 1
    client.post("/api/library/import", content=before.read_bytes())
    assert [s["title"] for s in new.songs()] == ["Something else"]


def _zip(**members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name.replace("__", "/"), data)
    return buf.getvalue()


@pytest.mark.parametrize("blob, message", [
    (b"not a zip", "not a zip"),
    (_zip(**{"library.sqlite3": b""}), "no manifest"),
    (_zip(**{"manifest.json": json.dumps({"format": 99})}), "can't read"),
    (_zip(**{"manifest.json": json.dumps({"format": 1})}), "no library"),
    (_zip(**{"manifest.json": json.dumps({"format": 1}), "library.sqlite3": b"garbage" * 200}), "can't be read"),
])
def test_bad_files_are_refused_and_change_nothing(tmp_path, monkeypatch, blob, message):
    lib, client = _app(tmp_path, monkeypatch)
    lib.add_song("Keep me", "Keep me", "")
    res = client.post("/api/library/import", content=blob)
    assert res.status_code == 400 and message in res.json()["detail"]
    assert [s["title"] for s in lib.songs()] == ["Keep me"]
    assert not list(tmp_path.glob("before-import-*"))


def test_paths_outside_the_folders_are_never_written(tmp_path, monkeypatch):
    old, client = _app(tmp_path / "old", monkeypatch)
    good = client.get("/api/library/export").content
    with zipfile.ZipFile(io.BytesIO(good)) as z:
        members = {n: z.read(n) for n in z.namelist()}
    members["NAM/../../escaped.txt"] = b"x"
    members["backups/../../../escaped2.txt"] = b"x"
    members["NAM/.hidden"] = b"x"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, data in members.items():
            z.writestr(n, data)
    _, client = _app(tmp_path / "new", monkeypatch)
    assert client.post("/api/library/import", content=buf.getvalue()).json()["files"] == 0
    assert not list(tmp_path.rglob("escaped*"))


def test_safe_member():
    assert library_io._safe_member("NAM/nam/a.nam", "NAM").as_posix() == "nam/a.nam"
    for bad in ("/NAM/a", "NAM/../a", "NAM\\..\\a", "other/a", "NAM", "NAM/.x"):
        assert library_io._safe_member(bad, "NAM") is None
