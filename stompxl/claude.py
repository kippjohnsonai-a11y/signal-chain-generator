"""Claude as a HelixGen LLM backend.

HelixGen drives every model round through one callable, ``LLMRequest -> str``:
a prompt plus the JSON schema the answer must match. This module supplies that
callable for Claude, using structured outputs so every answer parses.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import anthropic

from stompxl.helixgen.llm import LLMCaller, LLMGenerationError, LLMRequest

from . import config, costs

logger = logging.getLogger(__name__)

#: The model setting that follows Anthropic's newest Opus, looked up from the Models API.
LATEST_OPUS = "latest-opus"
DEFAULT_MODEL = LATEST_OPUS
#: Used when the Models API can't be reached.
FALLBACK_MODEL = "claude-opus-5-5"
#: Saved by older versions as the prefilled default: read it as "newest Opus".
_OLD_DEFAULTS = frozenset({"claude-opus-5"})
_MODELS_TTL = 3600.0
_models_cache: dict[str, Any] = {}
DEFAULT_EFFORT = "medium"
#: What a Claude feature says when there is no API key.
NO_KEY = ("Add an Anthropic API key in Settings to use Claude for this. "
          "Everything that doesn't need Claude works without one.")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Structured outputs accept a subset of JSON Schema. These keywords are
#: dropped before sending; HelixGen's parsers clamp and validate every value
#: afterwards anyway, so nothing they guarded is lost.
_UNSUPPORTED_KEYWORDS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
     "minLength", "maxLength", "minItems", "maxItems", "pattern"}
)

_SYSTEM = (
    "You are an expert guitar tone designer and Line 6 Helix / HX Stomp XL power user. "
    "You know the gear on famous recordings, how pickups interact with amps, and how "
    "to build practical, gig-ready presets. Answer only with the requested JSON."
)


def sanitize_schema(schema: Any) -> Any:
    """Rewrite a HelixGen schema into the subset structured outputs accepts.

    Numeric and length limits are removed, and ``"type": [T, "null"]`` unions
    become ``anyOf`` branches, carrying an ``enum`` onto the non-null branch.
    """

    if isinstance(schema, list):
        return [sanitize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    node = {
        key: sanitize_schema(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_KEYWORDS
    }
    kind = node.get("type")
    if isinstance(kind, list):
        others = [k for k in kind if k != "null"]
        branches: list[dict[str, Any]] = []
        for other in others:
            branch = {k: v for k, v in node.items() if k not in ("type", "enum")}
            branch["type"] = other
            if "enum" in node:
                values = [v for v in node["enum"] if v is not None]
                if values:
                    branch["enum"] = values
            if other != "object":
                for key in ("properties", "required", "additionalProperties"):
                    branch.pop(key, None)
            branches.append(branch)
        if "null" in kind:
            branches.append({"type": "null"})
        return branches[0] if len(branches) == 1 else {"anyOf": branches}
    return node


def get_client() -> anthropic.Anthropic:
    key = config.anthropic_api_key()
    # With no key, the SDK still finds credentials from `ant auth login`.
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def list_models(client: Any = None, *, refresh: bool = False) -> list[dict[str, str]]:
    """The Claude models this API key can use, newest first, from the Models API (cached an hour)."""
    now = time.monotonic()
    if not refresh and client is None and now - _models_cache.get("at", -_MODELS_TTL) < _MODELS_TTL:
        return list(_models_cache["models"])
    api = client or get_client()
    found = [m for m in api.models.list(limit=100) if str(m.id).startswith("claude-")]
    found.sort(key=lambda m: str(getattr(m, "created_at", "") or ""), reverse=True)
    models = [{"id": m.id, "name": getattr(m, "display_name", None) or m.id} for m in found]
    if client is None:
        _models_cache.update(at=now, models=models)
    return list(models)


def forget_models() -> None:
    """Drop the cached list, e.g. when the API key changes."""
    _models_cache.clear()


def latest_opus(models: list[dict[str, str]]) -> str:
    """The newest Opus in a newest-first model list."""
    return next((m["id"] for m in models if m["id"].startswith("claude-opus-")), FALLBACK_MODEL)


def resolve_model(setting: str | None, client: Any = None) -> str:
    """The model id to call for a saved setting: "latest-opus" (or nothing) is looked up."""
    chosen = (setting or "").strip()
    if chosen and chosen != LATEST_OPUS and chosen not in _OLD_DEFAULTS:
        return chosen
    try:
        return latest_opus(list_models(client))
    except Exception as exc:  # noqa: BLE001 - no network or no key yet: use the known default
        logger.warning("Couldn't list Claude models (%s); using %s.", exc, FALLBACK_MODEL)
        return FALLBACK_MODEL


def build_caller(
    *,
    model: str | None = None,
    effort: str | None = None,
    client: Any = None,
) -> LLMCaller:
    """Return a HelixGen ``LLMCaller`` backed by Claude."""

    effort_level = effort or config.get_setting("claude_effort") or DEFAULT_EFFORT
    if effort_level not in EFFORTS:
        effort_level = DEFAULT_EFFORT
    api = client or get_client()
    model_name = resolve_model(model or config.get_setting("claude_model"), client)

    def _call(llm_request: LLMRequest) -> str:
        started = time.monotonic()
        output_config: dict[str, Any] = {
            "effort": effort_level,
            "format": {
                "type": "json_schema",
                "schema": sanitize_schema(copy.deepcopy(llm_request.schema)),
            },
        }

        def _create(config_: dict[str, Any]) -> Any:
            return api.beta.messages.create(
                model=model_name,
                max_tokens=16000,
                system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config=config_,
                # Re-run a safety decline on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": llm_request.prompt}],
            )

        try:
            try:
                response = _create(output_config)
            except anthropic.BadRequestError as exc:
                if "schema" not in str(exc).lower():
                    raise
                # A schema too large for structured outputs: ask for the same JSON
                # in the prompt instead. HelixGen's parsers validate it either way.
                logger.warning("Schema refused (%s); retrying without it.", exc)
                response = _create({"effort": effort_level})
        except anthropic.AuthenticationError as exc:
            if not config.anthropic_api_key():
                raise LLMGenerationError(NO_KEY) from exc
            raise LLMGenerationError(
                "Claude rejected the API key. Add a valid key in Settings."
            ) from exc
        except TypeError as exc:
            # The SDK's way of saying it found no key and no sign-in at all.
            if "authentication" not in str(exc).lower():
                raise
            raise LLMGenerationError(NO_KEY) from exc
        except anthropic.RateLimitError as exc:
            raise LLMGenerationError("Claude rate limit hit; wait a moment and retry.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMGenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMGenerationError(f"Could not reach the Claude API: {exc}") from exc
        finally:
            logger.info(
                "Claude round '%s' took %.1fs", llm_request.schema_name, time.monotonic() - started
            )

        costs.record(str(getattr(response, "model", None) or model_name),
                     getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            raise LLMGenerationError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMGenerationError("Claude's answer was cut off (max_tokens).")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMGenerationError("Claude returned no text.")
        return text

    return _call
