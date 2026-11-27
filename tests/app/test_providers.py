import json

import httpx
import pytest

from stompxl import ai, claude, config, costs, providers
from stompxl.helixgen.llm import LLMGenerationError

from .test_claude import REQUEST


def _client(replies, seen):
    """An httpx client answering each request with the next (status, body) reply."""
    replies = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = replies.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _openai_reply(text, **extra):
    return {"model": "gpt-5.6-terra", "status": "completed",
            "output": [{"type": "reasoning", "content": []},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1_000_000, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 100_000}, **extra}


def _gemini_reply(text, finish="STOP"):
    return {"modelVersion": "gemini-3.8-flash",
            "candidates": [{"finishReason": finish, "content": {"parts": [
                {"text": "thinking...", "thought": True}, {"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 50_000,
                              "thoughtsTokenCount": 50_000}}


def test_strict_schema_requires_every_property_and_makes_optional_ones_nullable():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
              "required": ["a"]}
    out = providers.strict_schema(schema)
    assert out["required"] == ["a", "b"] and out["additionalProperties"] is False
    assert out["properties"]["b"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}


def test_openai_caller_sends_a_strict_schema_and_records_the_cost():
    seen = []
    call = providers.build_openai_caller(model="gpt-5.6-terra", effort="max",
                                         client=_client([(200, _openai_reply('{"x": 1}'))], seen))
    with costs.meter() as spent:
        assert json.loads(call(REQUEST)) == {"x": 1}
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/responses"
    assert sent["reasoning"] == {"effort": "xhigh"}
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["properties"]["x"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}]}
    assert sum(spent) == pytest.approx(2.0 + 1.2)  # Terra: $2 in, $12 out per million


def test_openai_retries_without_the_schema_when_refused():
    seen = []
    refused = (400, {"error": {"message": "Invalid schema for response_format"}})
    call = providers.build_openai_caller(client=_client([refused, (200, _openai_reply('{"x": 2}'))], seen))
    assert json.loads(call(REQUEST)) == {"x": 2}
    retry = json.loads(seen[1].content)
    assert retry["text"]["format"] == {"type": "json_object"} and "JSON Schema" in retry["input"]


def test_openai_errors_read_plainly(monkeypatch):
    monkeypatch.setattr(config, "api_key", lambda provider="anthropic": "sk-bad")
    call = providers.build_openai_caller(
        client=_client([(401, {"error": {"message": "Incorrect API key"}})], []))
    with pytest.raises(LLMGenerationError, match="rejected the API key"):
        call(REQUEST)
    refusal = _openai_reply("")
    refusal["output"] = [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
    with pytest.raises(LLMGenerationError, match="declined"):
        providers.build_openai_caller(client=_client([(200, refusal)], []))(REQUEST)


def test_gemini_caller_sends_the_schema_and_skips_thoughts():
    seen = []
    call = providers.build_gemini_caller(model="models/gemini-3.8-flash", effort="high",
                                         client=_client([(200, _gemini_reply('{"x": 3}'))], seen))
    with costs.meter() as spent:
        assert json.loads(call(REQUEST)) == {"x": 3}
    assert seen[0].url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    sent = json.loads(seen[0].content)
    assert sent["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    assert "minimum" not in json.dumps(sent["generationConfig"]["responseJsonSchema"])
    assert sum(spent) == pytest.approx(1.5 + 0.75)  # thinking bills as output


def test_gemini_cut_off_and_blocked_answers_are_errors():
    with pytest.raises(LLMGenerationError, match="cut off"):
        providers.build_gemini_caller(client=_client([(200, _gemini_reply("{", "MAX_TOKENS"))], []))(REQUEST)
    with pytest.raises(LLMGenerationError, match="declined"):
        providers.build_gemini_caller(
            client=_client([(200, {"promptFeedback": {"blockReason": "SAFETY"}})], []))(REQUEST)


def test_no_key_says_which_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMGenerationError, match="OpenAI API key"):
        providers.build_openai_caller()(REQUEST)


def test_model_lists_keep_text_models_newest_first():
    openai = _client([(200, {"data": [
        {"id": "gpt-5.6-luna", "created": 3}, {"id": "gpt-5.6-sol", "created": 5},
        {"id": "gpt-image-2", "created": 9}, {"id": "text-embedding-3", "created": 9}]})], [])
    assert [m["id"] for m in providers.list_models("openai", openai)] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    gemini = _client([(200, {"models": [
        {"name": "models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
         "supportedGenerationMethods": ["generateContent", "countTokens"]}]})], [])
    assert [m["id"] for m in providers.list_models("gemini", gemini)] == [
        "gemini-3.8-flash", "gemini-3.5-flash"]


def test_build_caller_follows_the_chosen_provider(monkeypatch):
    built = []
    monkeypatch.setattr(providers, "build_openai_caller", lambda: built.append("openai"))
    monkeypatch.setattr(providers, "build_gemini_caller", lambda: built.append("gemini"))
    monkeypatch.setattr(claude, "build_caller", lambda: built.append("anthropic"))
    for which in ("openai", "gemini", "anthropic"):
        config.write_settings({"ai_provider": which})
        ai.build_caller()
    config.write_settings({"ai_provider": "nonsense"})
    ai.build_caller()
    assert built == ["openai", "gemini", "anthropic", "anthropic"]


def test_each_provider_has_its_own_key_and_env_fallback(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-google")
    assert config.api_key("gemini") == "AIza-google" and config.api_key_source("gemini") == "env"
    config.write_settings({"gemini_api_key": "AIza-saved"})
    assert config.api_key("gemini") == "AIza-saved" and config.api_key_source("gemini") == "settings"
    assert config.api_key("openai") is None


def test_other_providers_are_priced():
    assert costs.price("gpt-5.6-luna-2026-07-30") == (0.20, 1.20)
    assert costs.price("gpt-7-something") == costs.PRICES["gpt-5.6-sol"]
    assert costs.price("gemini-3.1-pro-preview") == costs.PRICES["gemini-pro"]
    assert costs.price("gemini-3.5-flash-lite") == (0.30, 2.50)
