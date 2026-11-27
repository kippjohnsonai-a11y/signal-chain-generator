"""The AI the app builds presets with: Claude, OpenAI or Google Gemini.

The owner picks one in Settings and pastes that provider's key. Every feature
goes through :func:`build_caller`, so the rest of the app never asks which.
"""

from __future__ import annotations

from typing import Any

from stompxl.helixgen.llm import LLMCaller

from . import claude, config, providers

#: Shown in Settings, in this order. "ai" is what the app calls the assistant.
PROVIDERS: dict[str, dict[str, str]] = {
    "anthropic": {"label": "Claude (Anthropic)", "ai": "Claude", "company": "Anthropic",
                  "key_url": "https://console.anthropic.com/settings/keys",
                  "key_site": "console.anthropic.com", "key_hint": "sk-ant-...",
                  "env": "ANTHROPIC_API_KEY", "model_setting": "claude_model"},
    "openai": {"label": "ChatGPT (OpenAI)", "ai": "ChatGPT", "company": "OpenAI",
               "key_url": "https://platform.openai.com/api-keys",
               "key_site": "platform.openai.com", "key_hint": "sk-...",
               "env": "OPENAI_API_KEY", "model_setting": "openai_model"},
    "gemini": {"label": "Gemini (Google)", "ai": "Gemini", "company": "Google",
               "key_url": "https://aistudio.google.com/apikey",
               "key_site": "aistudio.google.com", "key_hint": "AIza...",
               "env": "GEMINI_API_KEY", "model_setting": "gemini_model"},
}
DEFAULT_PROVIDER = "anthropic"


def provider() -> str:
    return config.ai_provider()


def build_caller(*, which: str | None = None) -> LLMCaller:
    """A HelixGen caller for the chosen provider."""
    chosen = which or provider()
    if chosen == "openai":
        return providers.build_openai_caller()
    if chosen == "gemini":
        return providers.build_gemini_caller()
    return claude.build_caller()


def has_key(which: str | None = None) -> bool:
    return bool(config.api_key(which or provider()))


def model_setting(which: str) -> str:
    return PROVIDERS[which]["model_setting"]


def default_model(which: str) -> str:
    return claude.DEFAULT_MODEL if which == "anthropic" else providers.DEFAULT_MODELS[which]


def models(which: str, *, refresh: bool = False) -> dict[str, Any]:
    """The model dropdown for a provider: live list plus the recommended choice."""
    if which == "anthropic":
        try:
            found = claude.list_models(refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - no key yet, or no network
            return {"models": [], "latest_opus": claude.FALLBACK_MODEL,
                    "recommended": {"id": claude.LATEST_OPUS,
                                    "name": f"Newest Opus (now {claude.FALLBACK_MODEL})"},
                    "error": str(exc)}
        latest = claude.latest_opus(found)
        return {"models": found, "latest_opus": latest,
                "recommended": {"id": claude.LATEST_OPUS, "name": f"Newest Opus (now {latest})"},
                "error": None}
    default = providers.DEFAULT_MODELS[which]
    recommended = {"id": default, "name": default}
    try:
        found = providers.list_models(which, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        return {"models": [], "recommended": recommended, "error": str(exc)}
    return {"models": [m for m in found if m["id"] != default], "recommended": recommended,
            "error": None}


def forget_models(which: str | None = None) -> None:
    if which in (None, "anthropic"):
        claude.forget_models()
    if which != "anthropic":
        providers.forget_models(which)


def settings_view() -> dict[str, Any]:
    """What Settings shows about each provider: never the keys themselves."""
    saved = config.read_settings()
    return {
        name: {
            **{k: v for k, v in info.items() if k != "model_setting"},
            "has_key": bool(config.api_key(name)),
            "key_from_env": config.api_key_source(name) == "env",
            "model": saved.get(info["model_setting"]) or default_model(name),
        }
        for name, info in PROVIDERS.items()
    }
