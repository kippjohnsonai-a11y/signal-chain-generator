"""The desktop shortcut's launch: reuse a running copy, replace an old one."""

import os

import pytest

from stompxl import config, launch


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(launch, "this_version", lambda: "0.2.0")
    return tmp_path


def test_nothing_running_starts_a_new_server(monkeypatch):
    monkeypatch.setattr(launch, "running_version", lambda url, timeout=1.0: None)
    opened = []
    assert launch.already_running("http://127.0.0.1:8765/", opened.append) is False
    assert opened == []


def test_the_same_version_running_is_just_opened(monkeypatch):
    monkeypatch.setattr(launch, "running_version", lambda url, timeout=1.0: "0.2.0")
    opened = []
    assert launch.already_running("http://127.0.0.1:8765/", opened.append) is True
    assert opened == ["http://127.0.0.1:8765/"]


def test_an_older_version_is_stopped_first(monkeypatch):
    answers = iter(["0.1.0", None])
    monkeypatch.setattr(launch, "running_version", lambda url, timeout=1.0: next(answers))
    killed = []
    monkeypatch.setattr(launch.os, "kill", lambda pid, sig: killed.append(pid))
    launch.pid_path().write_text("4242", encoding="utf-8")
    assert launch.already_running("http://127.0.0.1:8765/", lambda url: None) is False
    assert killed == [4242]


def test_an_older_version_that_will_not_stop_is_reported(monkeypatch):
    monkeypatch.setattr(launch, "running_version", lambda url, timeout=1.0: "0.1.0")
    monkeypatch.setattr(launch.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    clock = iter(range(100))
    monkeypatch.setattr(launch.time, "monotonic", lambda: next(clock))
    launch.pid_path().write_text("4242", encoding="utf-8")
    with pytest.raises(SystemExit, match="version 0.1.0"):
        launch.already_running("http://127.0.0.1:8765/", lambda url: None)


def test_the_pid_file_is_ours_to_clear():
    launch.write_pid()
    assert launch.pid_path().read_text(encoding="utf-8") == str(os.getpid())
    launch.clear_pid()
    assert not launch.pid_path().exists()
    launch.pid_path().write_text("1", encoding="utf-8")
    launch.clear_pid()                      # another process's file stays
    assert launch.pid_path().exists()
