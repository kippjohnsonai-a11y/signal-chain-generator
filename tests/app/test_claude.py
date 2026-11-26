import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from stompxl.helixgen.llm import LLMGenerationError, LLMRequest
from stompxl import claude


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _reply(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


REQUEST = LLMRequest(prompt="hi", schema={"type": "object", "properties": {"x": {
    "type": ["number", "null"], "minimum": 0}}, "required": ["x"], "additionalProperties": False},
    schema_name="t")


def test_caller_sends_sanitized_schema_and_returns_text():
    client, messages = _client([_reply('{"x": 1}')])
    call = claude.build_caller(model="claude-sonnet-5-5", effort="medium", client=client)
    assert json.loads(call(REQUEST)) == {"x": 1}
    sent = messages.calls[0]
    assert sent["output_config"]["format"]["schema"]["properties"]["x"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}]}
    assert sent["fallbacks"] == "default" and sent["thinking"] == {"type": "adaptive"}


def test_caller_retries_without_schema_when_refused():
    bad = anthropic.BadRequestError(
        "schema too complex", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
        body=None)
    client, messages = _client([bad, _reply('{"x": 2}')])
    call = claude.build_caller(client=client)
    assert json.loads(call(REQUEST)) == {"x": 2}
    assert "format" not in messages.calls[1]["output_config"]


def test_refusal_is_an_error():
    client, _ = _client([_reply("", stop="refusal")])
    with pytest.raises(LLMGenerationError, match="declined"):
        claude.build_caller(client=client)(REQUEST)


class _Models:
    def __init__(self, ids):
        self.ids, self.calls = ids, 0

    def list(self, limit=100):
        self.calls += 1
        return [SimpleNamespace(id=i, display_name=i.upper(), created_at=f"2026-0{n}")
                for n, i in enumerate(self.ids, start=1)]


def test_the_model_list_is_live_newest_first_and_claude_only():
    api = SimpleNamespace(models=_Models(["claude-opus-5", "claude-sonnet-5-5", "claude-opus-5-5", "other"]))
    models = claude.list_models(api)
    assert [m["id"] for m in models] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"]
    assert claude.latest_opus(models) == "claude-opus-5-5"
    assert claude.latest_opus([]) == claude.FALLBACK_MODEL


def test_newest_opus_follows_new_releases_and_explicit_choices_stay():
    api = SimpleNamespace(models=_Models(["claude-opus-5-5", "claude-opus-6"]))
    assert claude.resolve_model("latest-opus", api) == "claude-opus-6"
    assert claude.resolve_model(None, api) == "claude-opus-6"
    assert claude.resolve_model("claude-opus-5", api) == "claude-opus-6"  # the old prefilled default
    assert claude.resolve_model("claude-sonnet-5-5", api) == "claude-sonnet-5-5"
    broken = SimpleNamespace(models=SimpleNamespace(list=lambda limit=100: 1 / 0))
    assert claude.resolve_model("latest-opus", broken) == claude.FALLBACK_MODEL


def test_the_caller_uses_the_resolved_model():
    client, messages = _client([_reply('{"x": 1}')])
    client.models = _Models(["claude-opus-5-5"])
    claude.build_caller(model="latest-opus", client=client)(REQUEST)
    assert messages.calls[0]["model"] == "claude-opus-5-5"


def test_the_settings_dropdown_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from stompxl import config
    from stompxl.db import Library
    from stompxl.server import create_app

    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    client = TestClient(create_app(Library(tmp_path / "lib.sqlite3")))
    assert client.get("/api/state").json()["settings"]["claude_model"] == "latest-opus"
    monkeypatch.setattr(claude, "list_models", lambda **_: [{"id": "claude-opus-5-5", "name": "Opus"}])
    assert client.get("/api/claude-models").json()["latest_opus"] == "claude-opus-5-5"

    def no_key(**_):
        raise RuntimeError("no API key")

    monkeypatch.setattr(claude, "list_models", no_key)
    res = client.get("/api/claude-models").json()
    assert res["models"] == [] and res["latest_opus"] == claude.FALLBACK_MODEL and res["error"]
