"""Check for updates, remind later, and install one from the desktop apps."""

import sys

import pytest
from fastapi.testclient import TestClient

from stompxl import config, updates
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude
from .test_server import BLOCKS


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(updates, "current", lambda: "0.2.2")
    monkeypatch.delenv(updates.UV_ENV, raising=False)
    monkeypatch.delenv(updates.LAUNCHER_ENV, raising=False)
    updates._install.update(state="idle", error=None, version=None)
    return tmp_path


def pypi(monkeypatch, version):
    calls = []

    def fetch(timeout=5.0):
        calls.append(1)
        return version
    monkeypatch.setattr(updates, "fetch_latest", fetch)
    return calls


def launcher(monkeypatch, tmp_path):
    uv = tmp_path / "uv"
    uv.write_text("", encoding="utf-8")
    monkeypatch.setenv(updates.UV_ENV, str(uv))
    monkeypatch.setenv(updates.LAUNCHER_ENV, str(tmp_path / "launcher.sh"))
    return uv


def test_versions_compare_as_numbers():
    assert updates.newer("0.2.10", "0.2.9")
    assert updates.newer("1.0", "0.9.9")
    assert not updates.newer("0.2.2", "0.2.2")
    assert not updates.newer("0.3.0rc1", "0.2.2")   # pre-releases are never offered
    assert not updates.newer(None, "0.2.2")
    assert updates.newer("0.2.3", "abc1234")        # a git checkout's commit


def test_a_newer_release_is_shown_and_pypi_is_asked_once_a_day(monkeypatch):
    calls = pypi(monkeypatch, "0.2.3")
    s = updates.status(now=1000)
    assert s["available"] and s["show"] and s["latest"] == "0.2.3"
    updates.status(now=1000 + 3600)
    assert len(calls) == 1
    updates.status(now=1000 + 25 * 3600)
    assert len(calls) == 2


def test_no_reminder_when_up_to_date_or_pypi_is_unreachable(monkeypatch):
    pypi(monkeypatch, "0.2.2")
    assert not updates.status(now=1000)["show"]
    pypi(monkeypatch, None)
    assert not updates.status(force=True, now=2000)["show"]


def test_remind_me_later_hides_this_release_for_a_week(monkeypatch):
    pypi(monkeypatch, "0.2.3")
    updates.status(now=1000)
    updates.snooze(7, now=1000)
    assert not updates.status(now=1000 + 86400)["show"]
    assert updates.status(now=1000 + 8 * 86400)["show"]
    # A newer release than the one snoozed shows straight away.
    pypi(monkeypatch, "0.2.4")
    assert updates.status(force=True, now=1000 + 2 * 86400)["show"]


def test_turning_reminders_off_stops_the_check(monkeypatch):
    calls = pypi(monkeypatch, "0.2.3")
    updates.set_enabled(False)
    s = updates.status(now=1000)
    assert not s["enabled"] and not s["show"] and calls == []
    # Check now still works.
    assert updates.status(force=True, now=1000)["available"]


def test_without_a_launcher_it_says_how_to_update(monkeypatch):
    pypi(monkeypatch, "0.2.3")
    s = updates.status(now=1000)
    assert s["can_install"] is False and "Releases page" in s["how"]
    with pytest.raises(RuntimeError, match="wasn't started from the Mac app"):
        updates.start_install()


def test_a_git_checkout_never_offers_updates(monkeypatch):
    monkeypatch.setattr(updates, "current", lambda: "abc1234")
    calls = pypi(monkeypatch, "0.2.3")
    assert not updates.status(now=1000)["show"] and calls == []


def test_update_now_downloads_pins_and_relaunches(monkeypatch, tmp_path):
    pypi(monkeypatch, "0.2.3")
    uv = launcher(monkeypatch, tmp_path)
    ran, started = [], []
    monkeypatch.setattr(updates.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(updates.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw["env"])))
    monkeypatch.setattr(updates.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    result = updates.start_install()
    assert result["version"] == "0.2.3"
    assert ran == [[str(uv), "tool", "run", "--python", "3.13", "--from",
                    "signal-chain-generator==0.2.3", "signal-chain-generator", "--help"]]
    assert updates.pinned() == "0.2.3"
    assert updates._install["state"] == "restarting"
    (command, env), = started
    if sys.platform != "win32":
        assert command == ["/bin/bash", str(tmp_path / "launcher.sh")]
    assert env[updates.NO_OPEN_ENV] == "1"


def test_a_failed_download_keeps_this_version(monkeypatch, tmp_path):
    pypi(monkeypatch, "0.2.3")
    launcher(monkeypatch, tmp_path)
    monkeypatch.setattr(updates.subprocess, "run", lambda cmd, **kw: type(
        "R", (), {"returncode": 2, "stderr": "error: no network", "stdout": ""})())
    monkeypatch.setattr(updates.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    updates.start_install()
    assert updates._install["state"] == "failed" and "no network" in updates._install["error"]
    assert updates.pinned() is None


def test_the_shortcut_hands_over_to_the_version_it_updated_to(monkeypatch, tmp_path):
    uv = launcher(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert updates.hand_over(["--launch"]) is False            # nothing pinned
    updates.pinned_path().write_text("0.2.1\n", encoding="utf-8")
    assert updates.hand_over(["--launch"]) is False            # older than this one
    updates.pinned_path().write_text("0.2.3\n", encoding="utf-8")
    assert updates.hand_over(["--launch", "--uv", str(uv)]) is True
    assert started == [[str(uv), "tool", "run", "--python", "3.13", "--from",
                        "signal-chain-generator==0.2.3", "signal-chain-generator",
                        "--launch", "--uv", str(uv)]]


def test_the_api(monkeypatch, tmp_path):
    pypi(monkeypatch, "0.2.3")
    client = TestClient(create_app(Library(tmp_path / "lib.sqlite3"), caller_factory=lambda: FakeClaude(BLOCKS)))
    s = client.get("/api/update").json()
    assert s["show"] and s["current"] == "0.2.2"
    r = client.put("/api/update", json={"snooze_days": 7})
    assert r.status_code == 200, r.text
    assert not r.json()["show"]
    assert client.put("/api/update", json={"enabled": False}).json()["enabled"] is False
    assert client.post("/api/update/install").status_code == 409
