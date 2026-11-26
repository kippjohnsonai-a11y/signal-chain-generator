"""A small TONE3000 API client: sign in, search tones, and download A2 captures.

TONE3000's API (https://www.tone3000.com/api) uses OAuth 2.0 with PKCE. The
player makes a free key in TONE3000's Settings > API Keys and pastes the
publishable ``t3k_pub_...`` key into the app's settings. Signing in then opens
tone3000.com in the browser, which sends the player back to the app's own
``http://127.0.0.1:<port>/api/tone3000/callback`` (TONE3000 always accepts
loopback redirects, with nothing to register). Tokens are kept in
settings.json, which is readable by its owner only.

There is one TONE3000 sign-in per app: the owner's. Guests signed in through
Cloudflare can't finish a loopback sign-in, so their searches and downloads use
the owner's key and tokens (the files still land in the guest's own folder).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from . import config, users

API = "https://www.tone3000.com"
#: A2 models carry an A2 Lite model inside, which is what NAM A2 Lite plays.
A2 = "2"
KEY_SETTING = "tone3000_key"
USER_AGENT = "Signal-Chain-Generator/0.1 (+https://github.com/kippjohnsonai-a11y/signal-chain-generator)"
TOKENS_SETTING = "tone3000_tokens"
#: PKCE verifiers waiting for their callback, by state. One sign-in at a time is plenty.
_PENDING: dict[str, dict[str, str]] = {}


def _get(key: str, default: Any = None) -> Any:
    with users.acting_as(users.OWNER):
        return config.get_setting(key, default)


def _write(values: dict[str, Any]) -> None:
    with users.acting_as(users.OWNER):
        config.write_settings(values)


def has_key() -> bool:
    return bool(str(_get(KEY_SETTING) or "").strip())


class Tone3000Error(RuntimeError):
    pass


class NotSignedIn(Tone3000Error):
    pass


# -- sign in -------------------------------------------------------------------


def publishable_key() -> str:
    key = str(_get(KEY_SETTING) or "").strip()
    if not key:
        raise NotSignedIn("Add your TONE3000 publishable key (t3k_pub_...) in Settings first. "
                          "Make one free at tone3000.com > Settings > API Keys.")
    return key


def signed_in() -> bool:
    return bool((_get(TOKENS_SETTING) or {}).get("refresh_token"))


def authorize_url(redirect_uri: str) -> str:
    """Where to send the browser to sign in. The callback then calls finish_sign_in."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _PENDING.clear()
    _PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri}
    query = urllib.parse.urlencode({
        "client_id": publishable_key(), "redirect_uri": redirect_uri, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    return f"{API}/api/v1/oauth/authorize?{query}"


def finish_sign_in(params: dict[str, str], post: Callable[..., dict[str, Any]] | None = None) -> None:
    """Handle the callback: check state, trade the code for tokens, save them."""
    if params.get("error"):
        raise Tone3000Error(f"TONE3000 said: {params['error']}")
    pending = _PENDING.pop(params.get("state") or "", None)
    if pending is None:
        raise Tone3000Error("This sign-in link is stale. Start it again from the app.")
    if not params.get("code"):
        raise Tone3000Error("Sign-in was canceled.")
    data = (post or _post_form)("/api/v1/oauth/token", {
        "grant_type": "authorization_code", "code": params["code"],
        "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"],
        "client_id": publishable_key(),
    })
    _save_tokens(data)


def sign_out() -> None:
    _write({TOKENS_SETTING: None})


def _save_tokens(data: dict[str, Any]) -> None:
    if not data.get("access_token"):
        raise Tone3000Error("TONE3000 didn't return a token.")
    old = _get(TOKENS_SETTING) or {}
    _write({TOKENS_SETTING: {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
    }})


# -- HTTP ----------------------------------------------------------------------


def _post_form(path: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        API + path, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise NotSignedIn("Your TONE3000 sign-in has expired. Sign in again in Settings.") from exc
        raise Tone3000Error(f"TONE3000 returned {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise Tone3000Error(f"Couldn't reach TONE3000: {exc}") from exc


class Client:
    """Signed-in API calls. ``opener`` is swapped out in tests."""

    def __init__(self, opener: Callable[[urllib.request.Request], bytes] | None = None,
                 post: Callable[..., dict[str, Any]] | None = None) -> None:
        self._open = opener or _open
        self._post = post or _post_form

    def _token(self, force_refresh: bool = False) -> str:
        tokens = _get(TOKENS_SETTING) or {}
        if not tokens.get("access_token"):
            raise NotSignedIn("Sign in to TONE3000 in Settings first.")
        if force_refresh or time.time() > float(tokens.get("expires_at") or 0) - 60:
            if not tokens.get("refresh_token"):
                raise NotSignedIn("Your TONE3000 sign-in has expired. Sign in again in Settings.")
            try:
                data = self._post("/api/v1/oauth/token", {
                    "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                    "client_id": publishable_key()})
            except NotSignedIn:
                sign_out()
                raise
            _save_tokens(data)
            tokens = _get(TOKENS_SETTING) or {}
        return str(tokens["access_token"])

    def get(self, path_or_url: str, params: dict[str, Any] | None = None, *, file: bool = False) -> bytes:
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        if not url.startswith(API + "/"):
            raise Tone3000Error(f"Not a TONE3000 address: {url}")
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        for attempt in (0, 1):
            request = urllib.request.Request(url, headers={
                "Accept": "*/*" if file else "application/json", "User-Agent": USER_AGENT})
            # A model download redirects to file storage, which refuses a request that
            # still carries TONE3000's token: keep it on the first hop only.
            request.add_unredirected_header(
                "Authorization", f"Bearer {self._token(force_refresh=attempt == 1)}")
            try:
                return self._open(request)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    continue
                if exc.code == 404:
                    raise Tone3000Error("TONE3000 can't find that tone (private or deleted?).") from exc
                if exc.code == 429:
                    raise Tone3000Error("TONE3000's rate limit: wait a minute and try again.") from exc
                raise Tone3000Error(f"TONE3000 returned {exc.code} for {url}{_where(exc, url)}") from exc
            except OSError as exc:
                raise Tone3000Error(f"Couldn't reach TONE3000: {exc}") from exc
        raise NotSignedIn("Your TONE3000 sign-in has expired. Sign in again in Settings.")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return json.loads(self.get(path, params))
        except ValueError as exc:
            raise Tone3000Error(f"TONE3000 sent something unexpected for {path}") from exc

    def search(self, query: str, page_size: int = 10) -> list[dict[str, Any]]:
        """NAM amp and full-rig tones with A2 models, most downloaded first."""
        data = self.get_json("/api/v1/tones/search", {
            "query": query, "sort": "downloads-all-time", "format": "nam", "architecture": A2,
            "gears": "amp_amp-cab", "page_size": min(page_size, 25)})
        return list(data.get("data") or [])

    def tone(self, tone_id: int) -> dict[str, Any]:
        return dict(self.get_json(f"/api/v1/tones/{tone_id}", {"architecture": A2}))

    def models(self, tone_id: int) -> list[dict[str, Any]]:
        data = self.get_json("/api/v1/models", {"tone_id": tone_id, "architecture": A2, "page_size": 300})
        return [m for m in data.get("data") or [] if m.get("model_url")]

    def download(self, model: dict[str, Any]) -> bytes:
        return self.get(str(model["model_url"]), file=True)


def _where(exc: urllib.error.HTTPError, url: str) -> str:
    """Where a refusal came from, and what it said, for working out why."""
    parts = []
    final = getattr(exc, "url", None) or exc.geturl()
    if final and final != url:
        parts.append(f"after a redirect to {urllib.parse.urlparse(final).netloc}")
    try:
        body = exc.read(300).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - the detail is only a hint
        body = ""
    if body:
        parts.append(" ".join(body.split())[:200])
    return f" ({'; '.join(parts)})" if parts else ""


def _open(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


# -- helpers -------------------------------------------------------------------


def tone_id(ref: str | int) -> int:
    """A tone's id from its id, or its tone3000.com/tones/<slug>-<id> link."""
    text = str(ref).strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"tone3000\.com/tones/[^?#\s]*?-(\d+)/?(?:[?#].*)?$", text)
    if not match:
        raise Tone3000Error(f"Not a TONE3000 tone link: {text}")
    return int(match.group(1))


def pick_model(models: list[dict[str, Any]], hint: str = "") -> dict[str, Any]:
    """The model whose name best matches ``hint`` (every word, then most words); else the first."""
    if not models:
        raise Tone3000Error("That tone has no A2 models, so nothing NAM A2 Lite can play.")
    words = [w for w in re.split(r"[^a-z0-9+]+", hint.lower()) if w]
    if not words:
        return models[0]

    def score(model: dict[str, Any]) -> tuple[int, int]:
        name = str(model.get("name") or "").lower()
        if hint.lower().strip() == name.strip():
            return (len(words) + 1, 0)
        return (sum(w in name for w in words), -len(name))

    return max(models, key=score)


def describe(tone: dict[str, Any]) -> dict[str, Any]:
    """The bits of a tone worth showing when picking one."""
    user = tone.get("user") or {}
    return {
        "id": tone.get("id"), "title": tone.get("title"), "url": tone.get("url"),
        "creator": user.get("display_name") or user.get("username"),
        "downloads": tone.get("downloads_count"), "favorites": tone.get("favorites_count"),
        "a2_models": tone.get("a2_models_count"), "full_rig": tone.get("gear") in ("amp-cab", "full-rig"),
        "makes": [m.get("name") for m in tone.get("makes") or []],
    }
