"""Where the app keeps its data, and the handful of settings set once.

Everything lives under one data directory: the SQLite library, device backups,
and ``settings.json`` (which may hold the AI provider's API keys, so it is written
readable by the owner only).

A guest signed in through Cloudflare (see :mod:`stompxl.users`) gets a data
directory of their own inside the owner's, so everything below follows them.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from . import APP_NAME

DATA_DIR_ENV = "STOMPXL_DATA_DIR"

#: The data folder's name before the app was renamed (Nov 2026).
LEGACY_FOLDER = "StompXL Tone Studio"


def base_dir() -> Path:
    """The owner's data directory, whoever is asking."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        support = Path.home() / "Library" / "Application Support"
        # Installs from before the rename keep their library where it is.
        legacy = support / LEGACY_FOLDER
        path = legacy if legacy.is_dir() else support / APP_NAME
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        path = (Path(appdata) if appdata else Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        path = Path.home() / ".stompxl"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """The data directory of whoever this request is for."""
    from . import users

    base = base_dir()
    if not users.is_guest():
        return base
    path = users.guest_dir(base, users.current())
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "library.sqlite3"


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_settings() -> dict[str, Any]:
    return _read(_settings_path())


def get_setting(key: str, default: Any = None) -> Any:
    return read_settings().get(key, default)


def write_settings(values: dict[str, Any]) -> None:
    settings = read_settings()
    for key, value in values.items():
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
    path = _settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)


#: Each AI provider's saved-key setting and the environment variables it falls back to.
API_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "anthropic": ("anthropic_api_key", ("ANTHROPIC_API_KEY",)),
    "openai": ("openai_api_key", ("OPENAI_API_KEY",)),
    "gemini": ("gemini_api_key", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
}


def _env_key(provider: str) -> str | None:
    return next((os.environ[n] for n in API_KEYS[provider][1] if os.environ.get(n)), None)


def api_key_source(provider: str = "anthropic") -> str | None:
    """Where a provider's key comes from: "settings", "env", or None.

    A key saved in Settings wins, so this app can use its own key without
    touching an ANTHROPIC_API_KEY (or OPENAI_API_KEY, GEMINI_API_KEY) that
    other tools on the computer rely on.
    """
    if _read(base_dir() / "settings.json").get(API_KEYS[provider][0]):
        return "settings"
    return "env" if _env_key(provider) else None


def api_key(provider: str = "anthropic") -> str | None:
    """The saved key, else the provider's environment variable.

    Guests use the owner's keys: they can't see or set ones of their own.
    """
    return (_read(base_dir() / "settings.json").get(API_KEYS[provider][0])
            or _env_key(provider) or None)


def anthropic_api_key_source() -> str | None:
    return api_key_source("anthropic")


def anthropic_api_key() -> str | None:
    return api_key("anthropic")


def ai_provider() -> str:
    """The AI the owner picked in Settings: "anthropic", "openai" or "gemini".

    Guests follow the owner's choice, as they use the owner's key.
    """
    chosen = _read(base_dir() / "settings.json").get("ai_provider")
    return chosen if chosen in API_KEYS else "anthropic"
