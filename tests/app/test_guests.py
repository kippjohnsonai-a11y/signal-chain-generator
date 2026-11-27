"""Guests signing in with Google through Cloudflare Access, each with their own library."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from stompxl.helixgen.device import relay, usb
from stompxl import access, config, live, pedal, users
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude
from .test_usb_relay import XL

TEAM = "example-band.cloudflareaccess.com"
AUD = "aud-tag-123"
CF_PORT = 8781
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    """Stands in for PyJWKClient: hands back the one public key Access signs with."""

    class _Signing:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, _token):
        return self._Signing()


def token(email, *, aud=AUD, key=KEY, expires_in=600, iss=f"https://{TEAM}"):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now,
                       "exp": now + expires_in}, key, algorithm="RS256")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(access.ALLOWED_ENV, "owner@example.com")
    monkeypatch.setenv(access.CF_PORT_ENV, str(CF_PORT))
    monkeypatch.setenv(access.CF_TEAM_ENV, TEAM)
    monkeypatch.setenv(access.CF_AUD_ENV, AUD)
    monkeypatch.setenv(access.GUESTS_ENV, "Sam@Gmail.com, jo@gmail.com")
    monkeypatch.setenv(access.OWNER_EMAILS_ENV, "owner@example.com")
    monkeypatch.setattr(access, "cloudflare_check", lambda: access.CloudflareCheck(TEAM, AUD, FakeJwks()))
    return create_app(Library(tmp_path / "lib.sqlite3"),
                      caller_factory=lambda: FakeClaude(["Scream 808", "Cali Rectifire",
                                                         "4x12 Cali V30", "Plate Reverb"]))


def tunnel(app, email=None, **kwargs):
    headers = {"Cf-Access-Jwt-Assertion": token(email, **kwargs)} if email else {}
    return TestClient(app, base_url=f"http://127.0.0.1:{CF_PORT}", headers=headers)


def owner(app):
    return TestClient(app, base_url="https://mini.tail1234.ts.net:8780",
                      headers={"X-Forwarded-For": "100.64.0.2", "Tailscale-User-Login": "owner@example.com"})


def test_the_tunnel_only_believes_a_verified_access_token(app):
    assert tunnel(app).get("/api/state").status_code == 401
    assert tunnel(app, "sam@gmail.com", aud="another-app").get("/api/state").status_code == 401
    assert tunnel(app, "sam@gmail.com", key=OTHER_KEY).get("/api/state").status_code == 401
    assert tunnel(app, "sam@gmail.com", expires_in=-120).get("/api/state").status_code == 401
    assert tunnel(app, "sam@gmail.com", iss="https://evil.cloudflareaccess.com").get(
        "/api/state").status_code == 401
    assert tunnel(app, "stranger@gmail.com").get("/api/state").status_code == 403
    assert tunnel(app, "sam@gmail.com").get("/api/state").status_code == 200
    assert tunnel(app).get("/api/version").status_code == 200

    # The Tailscale header and a loopback Host mean nothing on the tunnel's port.
    forged = TestClient(app, base_url=f"http://127.0.0.1:{CF_PORT}",
                        headers={"Tailscale-User-Login": "owner@example.com"})
    assert forged.get("/api/state").status_code == 401


def test_the_access_cookie_works_too(app):
    client = TestClient(app, base_url=f"http://127.0.0.1:{CF_PORT}")
    client.cookies.set("CF_Authorization", token("jo@gmail.com"))
    assert client.get("/api/state").json()["account"] == {
        "email": "jo@gmail.com", "guest": True, "claude_left": users.DEFAULT_DAILY_CLAUDE}


def test_each_guest_has_their_own_library(app, tmp_path):
    me, sam, jo = owner(app), tunnel(app, "sam@gmail.com"), tunnel(app, "jo@gmail.com")
    me.post("/api/guitars", json={"name": "Owner's Strat", "pickups": "SSS"})
    sam.post("/api/guitars", json={"name": "Sam's Tele", "pickups": "SS"})

    assert [g["name"] for g in me.get("/api/state").json()["guitars"]] == ["Owner's Strat"]
    assert [g["name"] for g in sam.get("/api/state").json()["guitars"]] == ["Sam's Tele"]
    assert jo.get("/api/state").json()["guitars"] == []
    assert (tmp_path / "users" / "sam@gmail.com" / "library.sqlite3").exists()

    # Owner's own Google account through the tunnel opens Owner's library.
    via_google = tunnel(app, "owner@example.com").get("/api/state").json()
    assert [g["name"] for g in via_google["guitars"]] == ["Owner's Strat"]
    assert via_google["account"]["guest"] is False


def test_guests_use_the_owners_key_but_cannot_change_it(app):
    config.write_settings({"anthropic_api_key": "sk-abc"})
    sam = tunnel(app, "sam@gmail.com")
    assert sam.get("/api/state").json()["settings"]["has_api_key"] is True
    sam.put("/api/settings", json={"anthropic_api_key": "sk-x", "gate_mode": "off"})
    assert config.get_setting("anthropic_api_key") == "sk-abc"
    with users.acting_as("sam@gmail.com"):
        assert config.anthropic_api_key() == "sk-abc"
        assert config.get_setting("gate_mode") == "off"
    assert config.get_setting("gate_mode") is None


def test_guests_follow_the_owners_ai_provider(app):
    config.write_settings({"ai_provider": "openai", "openai_api_key": "sk-owner"})
    sam = tunnel(app, "sam@gmail.com")
    sam.put("/api/settings", json={"ai_provider": "gemini", "openai_api_key": "sk-sam"})
    settings = sam.get("/api/state").json()["settings"]
    assert settings["ai_provider"] == "openai" and settings["has_api_key"] is True
    assert config.get_setting("openai_api_key") == "sk-owner"


def _job(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_guests_get_a_daily_allowance_of_claude_jobs(app, monkeypatch):
    monkeypatch.setenv(users.DAILY_CLAUDE_ENV, "1")
    sam = tunnel(app, "sam@gmail.com")
    state = sam.get("/api/state").json()
    guitar = sam.post("/api/guitars", json={"name": "Tele", "pickups": "SS"}).json()["id"]
    body = {"song": "Back in Black by AC/DC", "guitar_id": guitar, "rig_id": state["rigs"][0]["id"]}

    first = sam.post("/api/generate", json=body).json()["job_id"]
    assert _job(sam, first)["status"] == "done"
    # Someone else can't read Sam's job.
    assert tunnel(app, "jo@gmail.com").get(f"/api/jobs/{first}").status_code == 404
    assert owner(app).get(f"/api/jobs/{first}").status_code == 404

    second = _job(sam, sam.post("/api/generate", json=body).json()["job_id"])
    assert second["status"] == "error" and "today's 1 AI jobs" in second["error"]
    assert sam.get("/api/state").json()["account"]["claude_left"] == 0

    # The owner is never limited.
    me = owner(app)
    my_guitar = me.post("/api/guitars", json={"name": "Strat", "pickups": "SSS"}).json()["id"]
    for _ in range(2):
        job = me.post("/api/generate", json={**body, "guitar_id": my_guitar}).json()["job_id"]
        assert _job(me, job)["status"] == "done"


def test_a_guest_only_ever_reaches_their_own_pedal(monkeypatch):
    transport = object()
    with users.acting_as("sam@gmail.com"):
        # No relay: the pedal on the Mac mini's own USB is never offered.
        monkeypatch.setattr(usb, "_require_pyusb", lambda: pytest.fail("touched local USB"))
        assert usb.find_devices() == []
        with pytest.raises(usb.DeviceError, match="No pedal connected"):
            usb.Session.open()
        relay.attach(XL, transport, "Sam's phone")
        assert relay.current()[2] == "Sam's phone"
    try:
        assert relay.current() is None
        with users.acting_as("jo@gmail.com"):
            assert relay.current() is None
    finally:
        relay.detach(transport)
    with users.acting_as("sam@gmail.com"):
        assert relay.current() is None


def test_one_persons_live_edit_does_not_hold_anyone_elses_pedal():
    with users.acting_as("sam@gmail.com"):
        pedal._live.set()
        with pytest.raises(pedal.PedalError), pedal._exclusive():
            pass
    try:
        with pedal._exclusive():
            pass  # the owner's pedal is free
        assert live.current() is None
    finally:
        with users.acting_as("sam@gmail.com"):
            pedal._live.clear()


def test_guest_folder_names_are_safe():
    assert users.folder_name("Sam.Smith+band@Gmail.com") == "sam.smith+band@gmail.com"
    assert users.folder_name("../../etc") == "_.._etc"
    assert "/" not in users.folder_name("a/b@gmail.com")


def test_guests_share_the_owners_tone3000_sign_in_but_keep_their_own_files(app, tmp_path):
    from stompxl import namlib, tone3000

    config.write_settings({tone3000.KEY_SETTING: "t3k_pub_abc", tone3000.TOKENS_SETTING: {
        "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}})
    sam = tunnel(app, "sam@gmail.com")
    settings = sam.get("/api/state").json()["settings"]
    assert settings["tone3000_key"] is True and settings["tone3000_signed_in"] is True

    with users.acting_as("sam@gmail.com"):
        assert tone3000.publishable_key() == "t3k_pub_abc"
        assert tone3000.Client()._token() == "a"
        assert namlib.folder() == tmp_path / "users" / "sam@gmail.com" / "NAM"

    # Guests can't sign the owner out, replace the key, or open Finder on the Mac.
    assert sam.post("/api/tone3000/signout").status_code == 403
    assert sam.get("/api/tone3000/signin").status_code == 403
    sam.put("/api/settings", json={"tone3000_key": "t3k_pub_x"})
    assert config.get_setting(tone3000.KEY_SETTING) == "t3k_pub_abc"
    assert sam.post("/api/nam/open-folder").status_code == 403


def test_the_ir_files_download_as_a_zip(app):
    import io
    import zipfile

    from stompxl import namlib

    sam = tunnel(app, "sam@gmail.com")
    assert sam.get("/api/nam/download").status_code == 404
    with users.acting_as("sam@gmail.com"):
        namlib.ir_dir().mkdir(parents=True)
        (namlib.ir_dir() / "101 Plexi.wav").write_bytes(b"RIFF")
        namlib.nam_dir().mkdir(parents=True)
        (namlib.nam_dir() / "Plexi.nam").write_text("{}")
    response = sam.get("/api/nam/download")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert names == ["101 Plexi.wav", "nam/Plexi.nam"]
    # Nobody else gets Sam's files.
    assert tunnel(app, "jo@gmail.com").get("/api/nam/download").status_code == 404
