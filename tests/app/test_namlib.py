"""NAM captures from TONE3000: sign in, download, convert, list in settings."""

import json
import sys
import time
import urllib.parse

import pytest

from stompxl import config, nam2ir, namlib, tone3000, xl

MODELS = [
    {"id": 1, "name": "DI Clean", "model_url": "https://www.tone3000.com/api/v1/models/1/file"},
    {"id": 2, "name": "FR SM57 Crunch", "model_url": "https://www.tone3000.com/api/v1/models/2/file"},
]


class FakeClient:
    def __init__(self, gear="amp-cab", models=MODELS):
        self.gear, self._models, self.downloads = gear, models, []

    def tone(self, tone_id):
        if tone_id == 404:
            raise tone3000.Tone3000Error("TONE3000 can't find that tone")
        return {"id": tone_id, "title": "Plexi Pack", "gear": self.gear,
                "makes": [{"name": "Marshall Super Lead"}], "user": {"username": "someone"}}

    def models(self, tone_id):
        return self._models

    def download(self, model):
        self.downloads.append(model["name"])
        return json.dumps({"architecture": "WaveNet", "name": model["name"]}).encode()


def fake_convert(nam_path, wav_path):
    wav_path.write_bytes(b"RIFF" + nam_path.read_bytes()[:10])
    return "ok"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))


def test_tone_links_and_numbers_give_the_tone_id():
    assert tone3000.tone_id("https://www.tone3000.com/tones/marshall-plexi-super-lead-1959-el34-40971") == 40971
    assert tone3000.tone_id("https://www.tone3000.com/tones/vox-ac30-top-boost-full-rig-greenback--2696/?x=1") == 2696
    assert tone3000.tone_id(" 69206 ") == 69206
    with pytest.raises(tone3000.Tone3000Error, match="Not a TONE3000 tone link"):
        tone3000.tone_id("https://example.com/tones/x-12")


def test_the_model_hint_picks_the_closest_name():
    assert tone3000.pick_model(MODELS)["id"] == 1
    assert tone3000.pick_model(MODELS, "sm57")["id"] == 2
    assert tone3000.pick_model(MODELS, "fr crunch")["id"] == 2
    assert tone3000.pick_model(MODELS, "nothing like it")["id"] == 1
    with pytest.raises(tone3000.Tone3000Error, match="no A2 models"):
        tone3000.pick_model([])


def test_adding_a_capture_downloads_converts_and_lists_it():
    client = FakeClient()
    capture = namlib.add("https://www.tone3000.com/tones/plexi-40971", name="Plexi Crunch", slot=109,
                         model_hint="sm57", client=client, convert=fake_convert)
    assert client.downloads == ["FR SM57 Crunch"]
    assert capture["cab"] is True and capture["tone_id"] == 40971
    assert (namlib.nam_dir() / "Plexi Crunch.nam").is_file()
    wav = namlib.ir_dir() / "109 Plexi Crunch.wav"
    assert wav.is_file() and capture["ir_file"] == str(wav)
    listed = xl.parse_captures(config.get_setting("nam_captures"))
    assert [(c["slot"], c["name"], c["model"]) for c in listed] == [(109, "Plexi Crunch", "FR SM57 Crunch")]
    assert "Marshall Super Lead" in listed[0]["description"]

    # Another capture in the same slot replaces it, file and listing both.
    namlib.add(40971, name="Plexi Lead", slot=109, cab=False, client=client, convert=fake_convert)
    assert [p.name for p in namlib.ir_dir().glob("*.wav")] == ["109 Plexi Lead.wav"]
    assert [c["name"] for c in xl.parse_captures(config.get_setting("nam_captures"))] == ["Plexi Lead"]


def test_a_computer_only_capture_is_not_converted():
    def refuse(*_):
        raise AssertionError("converted a capture with no slot")

    capture = namlib.add(10912, client=FakeClient(gear="amp"), convert=refuse)
    assert capture["slot"] is None and capture["ir_file"] is None and capture["cab"] is False
    assert capture["name"] == "Plexi Pack"


def test_hx_edit_keeps_16_characters_so_wav_names_fit():
    assert namlib.wav_name(101, "Deluxe Reverb Clean") == "101 Deluxe Rever.wav"
    assert namlib.wav_name(7, "a/b:c") == "7 a-b-c.wav"


def test_one_failure_does_not_stop_the_rest():
    entries = [{"tone": 404, "name": "Gone", "slot": 101}, {"tone": 5, "name": "Fine", "slot": 102}]
    result = namlib.add_many(entries, client=FakeClient(), convert=fake_convert)
    assert [c["name"] for c in result["added"]] == ["Fine"]
    assert result["failed"][0]["name"] == "Gone"


def test_the_starter_library_fills_slots_101_to_120():
    entries = namlib.starter()
    assert [e["slot"] for e in entries] == list(range(101, 121))
    assert len({e["tone"] for e in entries}) == 20
    assert all(isinstance(e["tone"], int) and e["name"] and e["description"] for e in entries)
    assert all(len(namlib.wav_name(e["slot"], e["name"])) <= 20 for e in entries)


def test_the_converter_runs_as_its_own_program(tmp_path, monkeypatch):
    fake = tmp_path / "conv" / "tools"
    fake.mkdir(parents=True)
    (fake / "nam2ir.py").write_text(
        "import sys\n"
        "src, out = sys.argv[1], sys.argv[3]\n"
        "if 'bad' in src: sys.exit(src + ': no 3-channel WaveNet in ' + src)\n"
        "open(out, 'wb').write(b'x' * (44 + 2048 * 4))\n"
        "print('wrote', out)\n")
    monkeypatch.setenv(nam2ir.CONVERTER_DIR_ENV, str(tmp_path / "conv"))
    good, bad = tmp_path / "good.nam", tmp_path / "bad.nam"
    good.write_text("{}")
    bad.write_text("{}")
    nam2ir.convert(good, tmp_path / "out" / "101 Good.wav")
    assert (tmp_path / "out" / "101 Good.wav").stat().st_size == nam2ir.WAV_BYTES
    with pytest.raises(nam2ir.ConvertError, match=r"bad\.nam: no A2 Lite model inside"):
        nam2ir.convert(bad, tmp_path / "out" / "102 Bad.wav")
    assert not list((tmp_path / "out").glob("102*"))


def test_the_converter_is_pinned_and_lives_in_the_data_dir(tmp_path, monkeypatch):
    fetched = []

    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"# " + self.url.encode()

    def urlopen(url, timeout):
        fetched.append(url)
        return Response(url)

    monkeypatch.setattr(nam2ir.urllib.request, "urlopen", urlopen)
    root = nam2ir.converter_dir()
    assert root == tmp_path / "nam2ir" / nam2ir.CONVERTER_COMMIT[:12]
    assert all(nam2ir.CONVERTER_COMMIT in url for url in fetched)
    assert len(fetched) == len(nam2ir.CONVERTER_FILES)
    nam2ir.converter_dir()
    assert len(fetched) == len(nam2ir.CONVERTER_FILES)  # downloaded once


def test_signing_in_uses_pkce_and_a_loopback_redirect():
    with pytest.raises(tone3000.NotSignedIn, match="publishable key"):
        tone3000.authorize_url("http://127.0.0.1:8765/api/tone3000/callback")
    config.write_settings({tone3000.KEY_SETTING: "t3k_pub_abc"})
    url = tone3000.authorize_url("http://127.0.0.1:8765/api/tone3000/callback")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert query["client_id"] == "t3k_pub_abc" and query["code_challenge_method"] == "S256"

    sent = {}

    def post(path, fields):
        sent.update(fields)
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}

    with pytest.raises(tone3000.Tone3000Error, match="stale"):
        tone3000.finish_sign_in({"state": "wrong", "code": "c"}, post=post)
    tone3000.finish_sign_in({"state": query["state"], "code": "c"}, post=post)
    assert sent["code_verifier"] and sent["redirect_uri"].startswith("http://127.0.0.1")
    assert tone3000.signed_in()


def test_an_expired_token_is_refreshed_before_the_call():
    config.write_settings({tone3000.KEY_SETTING: "t3k_pub_abc", tone3000.TOKENS_SETTING: {
        "access_token": "old", "refresh_token": "r1", "expires_at": time.time() - 10}})
    seen = []

    def opener(request):
        seen.append(request.get_header("Authorization"))
        return json.dumps({"data": [{"id": 7, "title": "T", "gear": "amp"}]}).encode()

    def post(path, fields):
        assert fields["grant_type"] == "refresh_token"
        return {"access_token": "new", "expires_in": 3600}

    tones = tone3000.Client(opener=opener, post=post).search("plexi")
    assert tones[0]["id"] == 7 and seen == ["Bearer new"]
    assert config.get_setting(tone3000.TOKENS_SETTING)["refresh_token"] == "r1"


def test_the_client_only_sends_the_token_to_tone3000():
    config.write_settings({tone3000.KEY_SETTING: "t3k_pub_abc", tone3000.TOKENS_SETTING: {
        "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}})
    client = tone3000.Client(opener=lambda r: b"{}")
    with pytest.raises(tone3000.Tone3000Error, match="Not a TONE3000 address"):
        client.download({"model_url": "https://evil.example/x.nam"})


def test_settings_keep_where_a_capture_came_from(tmp_path):
    from fastapi.testclient import TestClient

    from stompxl.db import Library
    from stompxl.server import create_app

    client = TestClient(create_app(Library(tmp_path / "lib.sqlite3")))
    namlib.add(40971, name="Plexi", slot=109, client=FakeClient(), convert=fake_convert)
    # The Settings box sends only slot | name | description | cab.
    assert client.put("/api/settings", json={"nam_captures": [
        {"name": "Plexi", "slot": 110, "description": "edited"}, {"name": "New", "slot": None}]}).json()["ok"]
    listed = {c["name"]: c for c in client.get("/api/state").json()["settings"]["nam_captures"]}
    assert listed["Plexi"]["slot"] == 110 and listed["Plexi"]["tone_id"] == 40971
    assert "tone_id" not in listed["New"]

    refused = client.put("/api/settings", json={"tone3000_key": "t3k_cs_secret"})
    assert refused.status_code == 400
    assert client.put("/api/settings", json={"tone3000_key": "t3k_pub_x"}).json()["ok"]
    state = client.get("/api/state").json()["settings"]
    assert state["tone3000_key"] is True and state["tone3000_signed_in"] is False
    signin = client.get("/api/tone3000/signin").json()["url"]
    redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(signin).query))["redirect_uri"]
    assert redirect.endswith("/api/tone3000/callback")
    assert client.get("/api/tone3000/callback?state=nope&code=x").status_code == 400
    assert client.get("/api/tone3000/search?q=plexi").status_code == 401
    assert client.post("/api/nam/add", json={"tone": "not a link"}).status_code == 502


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_the_nam_folder_can_be_moved(tmp_path):
    config.write_settings({namlib.FOLDER_SETTING: str(tmp_path / "Elsewhere")})
    assert namlib.ir_dir() == tmp_path / "Elsewhere" / "HX IRs"


def test_a_download_keeps_the_token_off_the_storage_redirect(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen[self.path] = self.headers.get("Authorization")
            if self.path.startswith("/api/v1/models/1/download"):
                self.send_response(302)
                self.send_header("Location", "/storage/x.nam?sig=1")
                self.end_headers()
            elif self.path.startswith("/storage/") and not self.headers.get("Authorization"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"architecture": "WaveNet"}')
            else:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Invalid Compact JWS")

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(tone3000, "API", base)
    config.write_settings({tone3000.KEY_SETTING: "t3k_pub_abc", tone3000.TOKENS_SETTING: {
        "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}})
    try:
        data = tone3000.Client().download({"model_url": base + "/api/v1/models/1/download/x.nam"})
        assert data.startswith(b"{") and seen["/api/v1/models/1/download/x.nam"] == "Bearer a"
        assert seen["/storage/x.nam?sig=1"] is None
        with pytest.raises(tone3000.Tone3000Error, match=r"403 .*Invalid Compact JWS"):
            tone3000.Client().download({"model_url": base + "/api/v1/models/2/other"})
    finally:
        server.shutdown()


def test_the_full_rig_pack_fills_slots_121_up_with_captures_that_include_their_cab():
    entries = namlib.pack("full-rig")
    assert [e["slot"] for e in entries] == list(range(121, 121 + len(entries)))
    assert all(e["cab"] is True and isinstance(e["tone"], int) and e["name"] for e in entries)
    starter_slots = {e["slot"] for e in namlib.starter()}
    assert not starter_slots & {e["slot"] for e in entries}
    assert len({e["name"].lower() for e in entries + namlib.starter()}) == len(entries) + 20
    assert all(len(namlib.wav_name(e["slot"], e["name"])) <= 20 for e in entries)
    with pytest.raises(ValueError, match="No pack"):
        namlib.pack("nope")
