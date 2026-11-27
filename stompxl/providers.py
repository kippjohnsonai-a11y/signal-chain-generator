"""OpenAI and Google Gemini as HelixGen LLM backends, beside Claude.

Each provider supplies the same callable as :mod:`stompxl.claude`:
``LLMRequest -> str``, a JSON answer matching the request's schema. They talk
to each provider's REST API with httpx, so picking one needs no extra package.

Costs are recorded the same way as Claude's (see :mod:`stompxl.costs`).
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import httpx

from stompxl.helixgen.llm import LLMCaller, LLMGenerationError, LLMRequest

from . import config, costs
from .claude import _SYSTEM, DEFAULT_EFFORT, EFFORTS, sanitize_schema

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(600.0, connect=20.0)
_MAX_OUTPUT = 32000
_MODELS_TTL = 3600.0
_models_cache: dict[str, dict[str, Any]] = {}

OPENAI_URL = "https://api.openai.com/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

#: What a model setting is when nothing is picked: each provider's flagship.
DEFAULT_MODELS = {"openai": "gpt-5.6-sol", "gemini": "gemini-pro-latest"}
#: Model names that aren't for writing text (images, speech, embeddings...).
_NOT_TEXT = ("embed", "image", "tts", "audio", "realtime", "transcribe", "search",
             "moderation", "live", "veo", "imagen", "aqa", "dall-e", "whisper", "codex")


def no_key(provider: str) -> str:
    name = PROVIDER_NAMES[provider]
    return (f"Add a {name} API key in Settings to use {name} for this. "
            "Everything that doesn't need the AI works without one.")


PROVIDER_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google Gemini"}


def strict_schema(schema: Any) -> Any:
    """The schema in the shape OpenAI's strict mode needs.

    Every object lists all its properties as required and allows no others;
    a property that wasn't required becomes nullable instead.
    """
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    node = {k: strict_schema(v) for k, v in schema.items()}
    props = node.get("properties")
    if isinstance(props, dict):
        required = set(node.get("required") or [])
        for name, prop in list(props.items()):
            if name not in required and isinstance(prop, dict):
                props[name] = {"anyOf": [prop, {"type": "null"}]}
        node["required"] = list(props)
        node["additionalProperties"] = False
    return node


def _prepared(schema: dict[str, Any]) -> dict[str, Any]:
    return sanitize_schema(copy.deepcopy(schema))


def _json_prompt(llm_request: LLMRequest) -> str:
    """The prompt with the schema spelled out, for when the API won't take the schema."""
    import json

    return (f"{llm_request.prompt}\n\nAnswer with one JSON object, and nothing else, "
            f"matching this JSON Schema:\n{json.dumps(llm_request.schema)}")


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err or body)[:300]


def _raise_for(provider: str, response: httpx.Response) -> None:
    name = PROVIDER_NAMES[provider]
    status = response.status_code
    if status in (401, 403):
        if not config.api_key(provider):
            raise LLMGenerationError(no_key(provider))
        raise LLMGenerationError(f"{name} rejected the API key. Add a valid key in Settings.")
    if status == 429:
        message = _error_text(response)
        if "quota" in message.lower() or "billing" in message.lower():
            raise LLMGenerationError(f"{name} says the account is out of credit or quota: {message}")
        raise LLMGenerationError(f"{name} rate limit hit; wait a moment and retry.")
    raise LLMGenerationError(f"{name} API error {status}: {_error_text(response)}")


def _post(provider: str, client: httpx.Client, url: str, body: dict[str, Any]) -> httpx.Response:
    try:
        return client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise LLMGenerationError(f"Could not reach {PROVIDER_NAMES[provider]}: {exc}") from exc


def _client(provider: str) -> httpx.Client:
    key = config.api_key(provider)
    if not key:
        raise LLMGenerationError(no_key(provider))
    if provider == "openai":
        headers = {"Authorization": f"Bearer {key}"}
    else:
        headers = {"x-goog-api-key": key}
    return httpx.Client(timeout=_TIMEOUT, headers=headers)


def _schema_refused(response: httpx.Response) -> bool:
    return response.status_code == 400 and "schema" in _error_text(response).lower()


# ---------------------------------------------------------------- OpenAI


def _openai_effort(effort: str) -> str:
    # OpenAI's levels stop at xhigh.
    return "xhigh" if effort == "max" else effort


def _openai_text(body: dict[str, Any]) -> tuple[str | None, bool]:
    """The answer's text, and whether the model refused."""
    parts: list[str] = []
    refused = False
    for item in body.get("output") or []:
        for part in item.get("content") or [] if isinstance(item, dict) else []:
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part.get("type") == "refusal":
                refused = True
    return ("".join(parts).strip() or None), refused


def openai_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """OpenAI's token counts in the shape :func:`costs.dollars` reads."""
    usage = usage or {}
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return {"input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),
            "cache_read_input_tokens": cached,
            "output_tokens": int(usage.get("output_tokens") or 0)}


def build_openai_caller(*, model: str | None = None, effort: str | None = None,
                        client: httpx.Client | None = None) -> LLMCaller:
    effort_level = effort or config.get_setting("claude_effort") or DEFAULT_EFFORT
    if effort_level not in EFFORTS:
        effort_level = DEFAULT_EFFORT
    model_name = (model or config.get_setting("openai_model") or "").strip() or DEFAULT_MODELS["openai"]

    def _call(llm_request: LLMRequest) -> str:
        api = client or _client("openai")
        owned = client is None
        started = time.monotonic()
        body: dict[str, Any] = {
            "model": model_name,
            "instructions": _SYSTEM,
            "input": llm_request.prompt,
            "max_output_tokens": _MAX_OUTPUT,
            "reasoning": {"effort": _openai_effort(effort_level)},
            "text": {"format": {
                "type": "json_schema",
                "name": llm_request.schema_name or "answer",
                "schema": strict_schema(_prepared(llm_request.schema)),
                "strict": True,
            }},
        }
        try:
            response = _post("openai", api, f"{OPENAI_URL}/responses", body)
            if response.status_code == 400 and "reasoning" in _error_text(response).lower():
                # A model that doesn't take this thinking level: use its own default.
                body.pop("reasoning")
                response = _post("openai", api, f"{OPENAI_URL}/responses", body)
            if _schema_refused(response):
                logger.warning("OpenAI refused the schema (%s); retrying without it.",
                               _error_text(response))
                body["text"] = {"format": {"type": "json_object"}}
                body["input"] = _json_prompt(llm_request)
                response = _post("openai", api, f"{OPENAI_URL}/responses", body)
            if response.status_code >= 400:
                _raise_for("openai", response)
        finally:
            logger.info("OpenAI round '%s' took %.1fs", llm_request.schema_name,
                        time.monotonic() - started)
            if owned:
                api.close()
        data = response.json()
        costs.record(str(data.get("model") or model_name), openai_usage(data.get("usage")))
        text, refused = _openai_text(data)
        if refused:
            raise LLMGenerationError("OpenAI declined this request.")
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason") or "unknown"
            if reason == "max_output_tokens":
                raise LLMGenerationError("OpenAI's answer was cut off (too long).")
            if not text:
                raise LLMGenerationError(f"OpenAI stopped early ({reason}).")
        if not text:
            raise LLMGenerationError("OpenAI returned no text.")
        return text

    return _call


# ---------------------------------------------------------------- Gemini


def _gemini_thinking(effort: str) -> dict[str, Any] | None:
    # Gemini takes "low" or "high"; medium leaves the model's own default.
    if effort == "low":
        return {"thinkingLevel": "low"}
    if effort in ("high", "xhigh", "max"):
        return {"thinkingLevel": "high"}
    return None


def gemini_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Gemini's token counts in the shape :func:`costs.dollars` reads (thinking bills as output)."""
    usage = usage or {}
    cached = int(usage.get("cachedContentTokenCount") or 0)
    return {"input_tokens": max(0, int(usage.get("promptTokenCount") or 0) - cached),
            "cache_read_input_tokens": cached,
            "output_tokens": int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0)}


def build_gemini_caller(*, model: str | None = None, effort: str | None = None,
                        client: httpx.Client | None = None) -> LLMCaller:
    effort_level = effort or config.get_setting("claude_effort") or DEFAULT_EFFORT
    if effort_level not in EFFORTS:
        effort_level = DEFAULT_EFFORT
    model_name = (model or config.get_setting("gemini_model") or "").strip() or DEFAULT_MODELS["gemini"]
    model_name = model_name.removeprefix("models/")

    def _call(llm_request: LLMRequest) -> str:
        api = client or _client("gemini")
        owned = client is None
        url = f"{GEMINI_URL}/models/{model_name}:generateContent"
        started = time.monotonic()
        generation: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": _prepared(llm_request.schema),
            "maxOutputTokens": _MAX_OUTPUT,
        }
        thinking = _gemini_thinking(effort_level)
        if thinking:
            generation["thinkingConfig"] = thinking
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": llm_request.prompt}]}],
            "generationConfig": generation,
        }
        try:
            response = _post("gemini", api, url, body)
            if response.status_code == 400 and "thinking" in _error_text(response).lower():
                generation.pop("thinkingConfig", None)
                response = _post("gemini", api, url, body)
            if _schema_refused(response):
                logger.warning("Gemini refused the schema (%s); retrying without it.",
                               _error_text(response))
                generation.pop("responseJsonSchema", None)
                body["contents"][0]["parts"][0]["text"] = _json_prompt(llm_request)
                response = _post("gemini", api, url, body)
            if response.status_code >= 400:
                _raise_for("gemini", response)
        finally:
            logger.info("Gemini round '%s' took %.1fs", llm_request.schema_name,
                        time.monotonic() - started)
            if owned:
                api.close()
        data = response.json()
        costs.record(str(data.get("modelVersion") or model_name), gemini_usage(data.get("usageMetadata")))
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise LLMGenerationError(f"Gemini declined this request ({blocked}).")
        candidate = (data.get("candidates") or [{}])[0]
        finish = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if finish == "MAX_TOKENS":
            raise LLMGenerationError("Gemini's answer was cut off (too long).")
        if finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise LLMGenerationError(f"Gemini declined this request ({finish}).")
        if not text:
            raise LLMGenerationError("Gemini returned no text.")
        return text

    return _call


# ---------------------------------------------------------------- model lists


def _text_model(model_id: str) -> bool:
    return not any(word in model_id for word in _NOT_TEXT)


def list_models(provider: str, client: httpx.Client | None = None, *,
                refresh: bool = False) -> list[dict[str, str]]:
    """The models a provider's key can use for this, newest first (cached an hour)."""
    now = time.monotonic()
    cached = _models_cache.get(provider)
    if not refresh and client is None and cached and now - cached["at"] < _MODELS_TTL:
        return list(cached["models"])
    if client is None:
        with _client(provider) as api:
            models = _fetch_models(provider, api)
        _models_cache[provider] = {"at": now, "models": models}
        return list(models)
    return _fetch_models(provider, client)


def _fetch_models(provider: str, api: httpx.Client) -> list[dict[str, str]]:
    if provider == "openai":
        response = api.get(f"{OPENAI_URL}/models")
        if response.status_code >= 400:
            _raise_for(provider, response)
        found = [m for m in response.json().get("data") or []
                 if str(m.get("id", "")).startswith("gpt-") and _text_model(m["id"])]
        found.sort(key=lambda m: m.get("created") or 0, reverse=True)
        models = [{"id": m["id"], "name": m["id"]} for m in found]
    else:
        response = api.get(f"{GEMINI_URL}/models", params={"pageSize": 1000})
        if response.status_code >= 400:
            _raise_for(provider, response)
        models = []
        for m in response.json().get("models") or []:
            model_id = str(m.get("name", "")).removeprefix("models/")
            if (model_id.startswith("gemini-") and _text_model(model_id)
                    and "generateContent" in (m.get("supportedGenerationMethods") or [])):
                models.append({"id": model_id, "name": m.get("displayName") or model_id})
        # Google lists oldest first, roughly: newest first reads better.
        models.reverse()
    return models


def forget_models(provider: str | None = None) -> None:
    if provider:
        _models_cache.pop(provider, None)
    else:
        _models_cache.clear()
