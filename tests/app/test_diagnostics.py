"""The Send diagnostics button: what it gathers, what it blanks, where it goes."""

import json
import os
import urllib.error

import pytest
from fastapi.testclient import TestClient

from stompxl import config, diagnostics, users
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(diagnostics.TOKEN_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(diagnostics, "_failures", diagnostics.collections.deque(maxlen=5))
    monkeypatch.setattr(diagnostics, "_loaded", False)


@pytest.fixture
def client(tmp_path):
    app = create_app(Library(tmp_path / "lib.sqlite3"), caller_factory=lambda: FakeClaude([]))
    return TestClient(app)


def test_a_failure_is_kept_across_a_restart(tmp_path, monkeypatch):
    diagnostics.record_failure("Sending to slot 22B", TimeoutError("write timed out after 2000 ms"),
                               frames="  +0.000s OUT 32B ok src=0001", relayed="")
    saved = json.loads((tmp_path / "diagnostics" / "failures.json").read_text())
    assert saved[0]["what"] == "Sending to slot 22B"
    monkeypatch.setattr(diagnostics, "_failures", diagnostics.collections.deque(maxlen=5))
    monkeypatch.setattr(diagnostics, "_loaded", False)
    assert diagnostics.failures()[0]["error"] == "TimeoutError: write timed out after 2000 ms"


def test_secrets_and_emails_are_blanked(monkeypatch):
    config.write_settings({"anthropic_api_key": "sk-ant-api03-SECRETSECRET",
                           "diagnostics_token": "plainsecretvalue123"})
    monkeypatch.setenv("STOMPXL_GUEST_EMAILS", "friend@example.com,other@example.org")
    text = diagnostics.redact(
        "key sk-ant-api03-SECRETSECRET for friend@example.com, token=plainsecretvalue123 "
        "Authorization: Bearer abcdefghijklmnop ghp_abcdefghijklmnopqrstuvwxyz0123 "
        "frame src=0001 dst=0002 cmd=21")
    for secret in ("SECRETSECRET", "friend@example.com", "plainsecretvalue123",
                   "abcdefghijklmnop", "ghp_"):
        assert secret not in text
    assert "frame src=0001 dst=0002 cmd=21" in text


def test_preview_has_version_failure_and_error(client):
    diagnostics.record_failure("Sending to slot 22B", RuntimeError("boom"),
                               frames="  +0.000s OUT 32B ok src=0001", relayed="  write 32B ok")
    res = client.post("/api/diagnostics/preview",
                      json={"error": "Upload failed: boom", "note": "from the Mac",
                            "relay": {"active": True}}).json()
    assert res["can_send"] is False
    text = res["text"]
    assert "Commit:" in text and "Sending to slot 22B" in text and "src=0001" in text
    assert "Last relayed USB calls" in text and "Upload failed: boom" in text
    assert "from the Mac" in text
    # Without a token: GitHub's new-issue page, titled, asking for the report to be pasted.
    url = res["issue_url"]
    assert url.startswith(f"https://github.com/{diagnostics.repo()}/issues/new?title=Problem+report")
    assert "from+the+Mac" in url and "labels=diagnostics" in url and len(url) < 1000


def test_the_bundle_fits_an_issue():
    for i in range(5):
        diagnostics.record_failure(f"op {i}", RuntimeError("x"), frames="f" * 30_000)
    text = diagnostics.bundle({"commit": "abc"})
    assert len(text) <= diagnostics.MAX_BODY
    assert "op 4" in text  # the newest stays


def test_send_posts_an_issue(client, monkeypatch):
    client.put("/api/settings", json={"diagnostics_token": " github_pat_TESTTOKEN_1234567890abc "})
    assert client.get("/api/state").json()["settings"]["diagnostics_token"] is True
    assert "github_pat_TESTTOKEN" not in client.get("/api/state").text
    sent = {}

    def fake_post(url, payload, key):
        sent.update(url=url, payload=payload, key=key)
        return {"html_url": "https://github.com/o/r/issues/7"}

    monkeypatch.setattr(diagnostics, "_post", fake_post)
    res = client.post("/api/diagnostics/send", json={"note": "hi"}).json()
    assert res == {"ok": True, "url": "https://github.com/o/r/issues/7"}
    assert sent["key"] == "github_pat_TESTTOKEN_1234567890abc"
    assert sent["url"].endswith("/repos/kippjohnsonai-a11y/signal-chain-generator/issues")
    assert sent["payload"]["labels"] == ["diagnostics"]
    assert "github_pat_TESTTOKEN" not in sent["payload"]["body"]


def test_a_refused_send_still_returns_the_bundle(client, monkeypatch):
    monkeypatch.setenv(diagnostics.TOKEN_ENV, "github_pat_x")

    def refuse(url, payload, key):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(diagnostics, "_post", refuse)
    res = client.post("/api/diagnostics/send", json={}).json()
    assert res["ok"] is False and "can't write issues" in res["error"]
    assert "Commit:" in res["text"]


def test_guests_cannot_send_or_set_the_token(client, monkeypatch):
    monkeypatch.setattr(users, "is_guest", lambda: True)
    assert client.post("/api/diagnostics/preview", json={}).status_code == 403
    assert client.post("/api/diagnostics/send", json={}).status_code == 403
    client.put("/api/settings", json={"diagnostics_token": "github_pat_guest"})
    assert config._read(config.base_dir() / "settings.json").get("diagnostics_token") is None


# -- the pedal write test ----------------------------------------------------


@pytest.fixture
def fake_pedal(monkeypatch):
    """A pedal session that records what it was asked, failing where told."""
    from stompxl.helixgen.device import editor, usb, writer
    from stompxl import pedal

    calls: list[tuple] = []
    fail_on: dict[str, int] = {}

    class FakeSession:
        _device = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read_slot(self, *, bank, slot):
            calls.append(("read", slot))
            return b"donor-bytes"

    def write_document(self, blob, *, slot=0, bank=0):
        calls.append(("write", blob))
        writes = sum(1 for c in calls if c[0] == "write")
        if fail_on.get("write") == writes:
            raise usb.DeviceError("Short bulk write: 256 of 512 bytes")
        return writer.WriteStats(units=1, credit_latencies_ms=[2.0])

    monkeypatch.setattr(usb.Session, "open", staticmethod(lambda: FakeSession()))
    monkeypatch.setattr(editor.BlockEditor, "select_preset",
                        lambda self, bank, slot: calls.append(("select", slot)))
    monkeypatch.setattr(writer.DocumentWriter, "write_document", write_document)
    monkeypatch.setattr(pedal.time, "sleep", lambda _s: None)
    monkeypatch.setattr("stompxl.helixgen.device.document.parse", lambda raw: raw)
    monkeypatch.setattr("stompxl.helixgen.device.document.dump", lambda doc: b"re-encoded:" + doc)
    return calls, fail_on


def test_the_write_test_sends_the_slot_back_as_read_then_re_encoded_then_the_last_layout(
        fake_pedal):
    from stompxl import pedal

    calls, _ = fake_pedal
    (config.backups_dir() / "layout-slot-000-20261122-130250.msgpack").write_bytes(b"layout")
    result = pedal.write_test(0)
    writes = [c[1] for c in calls if c[0] == "write"]
    assert writes == [b"donor-bytes", b"re-encoded:donor-bytes", b"layout"]
    # Each write is loaded and read first, and loaded again after so nothing sticks.
    assert calls[:4] == [("select", 0), ("read", 0), ("write", b"donor-bytes"), ("select", 0)]
    assert [s["ok"] for s in result["steps"]] == [True, True, True]
    assert "slot 1A" in result["summary"]


def test_the_write_test_stops_at_the_first_refused_write(fake_pedal):
    from stompxl import pedal

    calls, fail_on = fake_pedal
    fail_on["write"] = 1
    result = pedal.write_test(0)
    assert [s["ok"] for s in result["steps"]] == [False]
    assert sum(1 for c in calls if c[0] == "write") == 1
    assert "Short bulk write" in result["summary"]
    assert "off and on" in result["summary"]
    assert diagnostics.failures()[-1]["what"] == "Write test on 1A, as read"


def test_the_write_test_skips_the_layout_when_there_was_no_send(fake_pedal):
    from stompxl import pedal

    result = pedal.write_test(5)
    assert [s["ok"] for s in result["steps"]] == [True, True, None]


def test_guests_cannot_run_the_write_test(client, monkeypatch):
    monkeypatch.setattr(users, "is_guest", lambda: True)
    assert client.post("/api/diagnostics/write-test", json={}).status_code == 403


def test_a_send_on_a_full_speed_link_stops_before_anything_changes(monkeypatch):
    """An old cable dropped the pedal to full speed on Nov 22: say so up front."""
    from stompxl.helixgen.device import editor, usb
    from stompxl import pedal

    class SlowSession:
        _device = None
        out_packet_size = 64

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read_slot(self, *, bank, slot):
            return b"donor-bytes"

    applied = []
    monkeypatch.setattr(usb.Session, "open", staticmethod(lambda: SlowSession()))
    monkeypatch.setattr("stompxl.helixgen.device.hxedit.find_symbol_table", lambda _p: None)
    monkeypatch.setattr("stompxl.helixgen.device.symbols.DeviceSymbols.load", lambda _p: object())
    monkeypatch.setattr(editor, "apply_tone", lambda *a, **k: applied.append(1))
    with pytest.raises(pedal.PedalError, match="cable"):
        pedal.push({"data": {"meta": {"name": "x"}}}, 0)
    assert applied == []
    assert "slow USB link" in diagnostics.failures()[-1]["error"]


def test_the_issue_title_says_what_happened_not_an_old_failure():
    diagnostics.record_failure("Write test on 1A, as read", RuntimeError("old"))
    assert diagnostics.headline("toggles didn't change the pedal", "") == \
        "toggles didn't change the pedal"
    assert diagnostics.headline("", "Couldn't open the pedal for live edit: timed out") \
        .startswith("Couldn't open the pedal")
    assert diagnostics.headline() == "last failure: Write test on 1A, as read"


def test_the_browser_s_recent_events_are_in_the_bundle(client):
    res = client.post("/api/diagnostics/preview", json={
        "events": ["19:18:02 live update: sent 0, waiting: snapshot 2"]}).json()
    assert "sent 0, waiting: snapshot 2" in res["text"]


def test_an_error_the_app_returns_is_logged_for_diagnostics(client):
    assert client.post("/api/diagnostics/write-test", json={"slot": 999}).status_code == 400
    assert any("/api/diagnostics/write-test -> 400: Slot out of range" in line
               for line in diagnostics._ring.lines)


def test_redact_hides_personal_details(monkeypatch, tmp_path):
    from pathlib import Path

    from stompxl import diagnostics

    home = Path("/Users/someone")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    log = f"{home}{os.sep}Library{os.sep}app.log"  # as this platform writes it
    text = diagnostics.redact("HX Stomp XL (serial 1234567) from jo.smith+x@mail.example.org "
                              f"at {log}")
    assert "1234567" not in text and "serial [hidden]" in text
    assert "jo.smith" not in text and "mail.example.org" not in text
    assert str(home) not in text and text.endswith(f"~{os.sep}Library{os.sep}app.log")
