"""Phones on the same Wi-Fi: off by default, and only paired devices get in."""

import json

import pytest
from fastapi.testclient import TestClient

from stompxl import access, config, lan
from stompxl.db import Library
from stompxl.server import create_app

PORT = 8790


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(access.ALLOWED_ENV, raising=False)
    started = []
    monkeypatch.setattr(lan, "start", lambda app: started.append(app))
    monkeypatch.setattr(lan, "stop", lambda: started.clear())
    monkeypatch.setattr(lan, "listening", lambda: bool(started))
    monkeypatch.setattr(lan, "running_port", lambda: PORT if started else None)
    monkeypatch.setattr(lan, "local_ip", lambda: "192.168.1.20")
    lan._codes.clear()
    return create_app(Library(tmp_path / "lib.sqlite3"))


def computer(app):
    return TestClient(app, base_url="http://127.0.0.1:8765")


def phone(app, headers=None):
    client = TestClient(app, base_url=f"http://192.168.1.20:{PORT}", follow_redirects=False,
                        headers=headers)
    client.headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0)"
    return client


def test_off_by_default_and_pairing_needs_it_on(app):
    status = computer(app).get("/api/lan").json()
    assert status["enabled"] is False and status["listening"] is False and status["devices"] == []
    assert computer(app).post("/api/lan/pair").status_code == 400


def test_pairing_a_phone_end_to_end(app):
    pc = computer(app)
    assert pc.put("/api/lan", json={"enabled": True}).json()["listening"] is True
    assert config.get_setting("lan_enabled") is True

    # Unpaired: nothing but the version check.
    stranger = phone(app)
    assert stranger.get("/api/state").status_code == 401
    assert stranger.get("/").status_code == 401
    assert stranger.get("/api/version").status_code == 200
    # Forged headers that count elsewhere mean nothing here.
    forged = phone(app, headers={"Tailscale-User-Login": "owner@example.com", "Host": "127.0.0.1"})
    assert forged.get("/api/state").status_code == 401

    pair = pc.post("/api/lan/pair").json()
    assert pair["url"] == f"http://192.168.1.20:{PORT}/pair?code={pair['code']}"
    assert pair["qr_svg"].startswith("<svg")

    mine = phone(app)
    res = mine.get(f"/pair?code={pair['code'].lower()}")
    assert res.status_code == 303 and res.headers["location"] == "/"
    cookie = res.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert mine.get("/api/state").status_code == 200
    # The code worked once.
    assert phone(app).get(f"/pair?code={pair['code']}").status_code == 403

    devices = pc.get("/api/lan").json()["devices"]
    assert [d["name"] for d in devices] == ["iPhone"]
    # Only a hash is stored, never the token.
    token = mine.cookies.get(lan.COOKIE)
    saved = (config.base_dir() / "devices.json").read_text()
    assert token not in saved and json.loads(saved)[0]["hash"]

    # A paired phone can't pair more devices or change the switch.
    assert mine.post("/api/lan/pair").status_code == 403
    assert mine.put("/api/lan", json={"enabled": False}).status_code == 403

    # Forgetting it ends its access; turning it off closes the port's gate.
    pc.delete(f"/api/lan/devices/{devices[0]['id']}")
    assert mine.get("/api/state").status_code == 401
    assert pc.put("/api/lan", json={"enabled": False}).json()["enabled"] is False
    assert lan.running_port() is None


def test_too_many_wrong_codes_cancel_the_waiting_ones(app):
    computer(app).put("/api/lan", json={"enabled": True})
    good = computer(app).post("/api/lan/pair").json()["code"]
    for _ in range(lan.MAX_BAD_CODES):
        assert phone(app).get("/pair?code=AAAA-AAAA").status_code == 403
    assert phone(app).get(f"/pair?code={good}").status_code == 403


def test_device_names():
    assert lan.device_name("Mozilla/5.0 (Linux; Android 15; Pixel 9)") == "Android phone"
    assert lan.device_name("") == "Phone"


def test_an_unpaired_browser_gets_a_code_box(app):
    """A Home Screen app on iPhone has its own cookies, so it pairs by typing the code."""
    pc = computer(app)
    pc.put("/api/lan", json={"enabled": True})
    browser = phone(app, headers={"Accept": "text/html,application/xhtml+xml"})
    page = browser.get("/")
    assert page.status_code == 401 and page.headers["content-type"].startswith("text/html")
    assert 'action="/pair"' in page.text and "Home Screen" in page.text
    # A script still gets plain text.
    assert phone(app).get("/").headers["content-type"].startswith("text/plain")

    bad = browser.get("/pair?code=nope")
    assert bad.status_code == 403 and 'name="code"' in bad.text

    code = pc.post("/api/lan/pair").json()["code"]
    typed = f" {code.replace('-', '').lower()[:4]} {code.replace('-', '').lower()[4:]} "
    res = browser.get("/pair", params={"code": typed})
    assert res.status_code == 303
    assert browser.get("/api/state").status_code == 200


def test_normalize_code():
    assert lan.normalize_code("abcd efgh") == "ABCD-EFGH"
    assert lan.normalize_code("ABCD-EFGH") == "ABCD-EFGH"
    assert lan.normalize_code(" abcdefgh ") == "ABCD-EFGH"
    assert lan.normalize_code("short") == "SHORT"
