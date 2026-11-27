"""Who may use the app when it is published to a tailnet.

On the Mac mini the app listens on 127.0.0.1 only, and Tailscale Serve puts it
on an HTTPS address that only devices on the tailnet can reach. Everyone on the
tailnet is not everyone who should drive the pedal or spend the API key, so
when ``STOMPXL_ALLOWED_LOGINS`` is set, every request must carry a
``Tailscale-User-Login`` header naming one of those logins. Tailscale Serve
adds that header itself (and replaces any a client sends), naming the person
signed in on the device the request came from.

Unset, nothing is checked: the plain ``uv run stompxl`` setup on one Mac keeps
working exactly as before.

``GET /api/version`` stays open, so the launcher on the Mac can tell whether
the server is up without signing in.

A browser on the Mac mini itself, opening ``http://127.0.0.1:<port>`` directly,
is let in too: some things only work on a loopback address (TONE3000 sign-in
redirects only to loopback). A request counts as direct only when it comes from
loopback, names a loopback host, and carries no ``X-Forwarded-For``. Tailscale
Serve proxies from loopback as well, but always adds ``X-Forwarded-For``, so
nothing that came through the tailnet passes as direct.

Guests (Google sign-in through Cloudflare)
-------------------------------------------
``cloudflared`` on the Mac mini forwards a public hostname to a second local
port, ``STOMPXL_CLOUDFLARE_PORT``, and Cloudflare Access in front of it asks
for a Google sign-in. Everything arriving on that port must carry the signed
token Access adds (``Cf-Access-Jwt-Assertion``, or the ``CF_Authorization``
cookie): it is checked against the team's public keys, its audience tag and its
expiry before its email is believed. The Tailscale header and the loopback rule
mean nothing on that port, so a guest can't pass as the owner by sending them.

The email then has to be on one of two lists. ``STOMPXL_GUEST_EMAILS`` get a
library of their own (see :mod:`stompxl.users`); ``STOMPXL_OWNER_EMAILS``
(optional) get the owner's, for using the app away from the tailnet.

Phones on the same Wi-Fi
------------------------
Off unless turned on in Settings. Requests on the LAN port (see
:mod:`stompxl.lan`) need a paired device's token, nothing else, and act as the
owner.
"""

from __future__ import annotations

import html
import logging
import os
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs

from anyio import to_thread

from . import APP_NAME, lan, users

logger = logging.getLogger(__name__)

ALLOWED_ENV = "STOMPXL_ALLOWED_LOGINS"
CF_PORT_ENV = "STOMPXL_CLOUDFLARE_PORT"
CF_TEAM_ENV = "STOMPXL_CF_TEAM_DOMAIN"
CF_AUD_ENV = "STOMPXL_CF_AUD"
GUESTS_ENV = "STOMPXL_GUEST_EMAILS"
OWNER_EMAILS_ENV = "STOMPXL_OWNER_EMAILS"
CF_HEADER = "cf-access-jwt-assertion"
CF_COOKIE = "CF_Authorization"
HEADER = "tailscale-user-login"
OPEN_PATHS = frozenset({"/api/version"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _emails(env: str) -> frozenset[str]:
    raw = os.environ.get(env, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def allowed_logins() -> frozenset[str]:
    return _emails(ALLOWED_ENV)


def cloudflare_port() -> int | None:
    raw = os.environ.get(CF_PORT_ENV, "").strip()
    return int(raw) if raw.isdigit() else None


class CloudflareCheck:
    """Verifies Cloudflare Access tokens for one Access application."""

    def __init__(self, team_domain: str, audience: str, jwks_client: Any = None):
        team = team_domain.strip().removeprefix("https://").rstrip("/")
        self.issuer = f"https://{team}"
        self.audience = audience.strip()
        if jwks_client is None:
            import jwt

            jwks_client = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                          cache_keys=True, lifespan=3600)
        self._jwks = jwks_client

    def email(self, token: str) -> str | None:
        """The verified email in ``token``, or None if it doesn't check out."""
        import jwt

        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat"]})
        except Exception as exc:  # noqa: BLE001 - any failure means "not signed in"
            logger.warning("Cloudflare Access token refused: %s", exc)
            return None
        email = str(claims.get("email") or "").strip().lower()
        return email or None


@lru_cache(maxsize=4)
def _cloudflare_check(team: str, audience: str) -> CloudflareCheck:
    return CloudflareCheck(team, audience)


def cloudflare_check() -> CloudflareCheck | None:
    team = os.environ.get(CF_TEAM_ENV, "").strip()
    audience = os.environ.get(CF_AUD_ENV, "").strip()
    return _cloudflare_check(team, audience) if team and audience else None


def _cookie(headers: dict[str, str], name: str) -> str:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


class TailnetLoginGate:
    """ASGI middleware enforcing :data:`ALLOWED_ENV` on HTTP and WebSocket."""

    def __init__(self, app: Any, allowed: frozenset[str] | None = None,
                 cloudflare: CloudflareCheck | None = None):
        self.app = app
        self.allowed = allowed_logins() if allowed is None else allowed
        self.cf_port = cloudflare_port()
        self._cloudflare = cloudflare

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        lan_port = lan.running_port()
        if scope["type"] in ("http", "websocket") and lan_port is not None \
                and (scope.get("server") or ("", None))[1] == lan_port:
            await _via_lan(self.app, scope, receive, send)
            return
        if scope["type"] in ("http", "websocket") and self.cf_port is not None \
                and (scope.get("server") or ("", None))[1] == self.cf_port:
            await self._via_cloudflare(scope, receive, send)
            return
        if scope["type"] not in ("http", "websocket") or not self.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {name.decode("latin-1").lower(): value.decode("latin-1")
                   for name, value in scope.get("headers") or []}
        login = headers.get(HEADER, "").strip().lower()
        if login in self.allowed or _direct_local(scope, headers):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        who = login or "someone not signed in to Tailscale"
        await _refuse(scope, send, 403, f"{APP_NAME} is private. {who} is not on its "
                                        f"list of allowed Tailscale logins ({ALLOWED_ENV}).")

    async def _via_cloudflare(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """A request from the Cloudflare tunnel: only a verified Access token counts."""
        if scope["type"] == "http" and scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        check = self._cloudflare or cloudflare_check()
        if check is None:
            await _refuse(scope, send, 503, f"Google sign-in isn't set up yet ({CF_TEAM_ENV} "
                                            f"and {CF_AUD_ENV}).")
            return
        headers = {name.decode("latin-1").lower(): value.decode("latin-1")
                   for name, value in scope.get("headers") or []}
        token = headers.get(CF_HEADER, "").strip() or _cookie(headers, CF_COOKIE)
        email = await to_thread.run_sync(check.email, token) if token else None
        if email is None:
            await _refuse(scope, send, 401, "Sign in again: your Google sign-in has expired or "
                                            "didn't come through.")
            return
        if email in _emails(OWNER_EMAILS_ENV):
            who = users.OWNER
        elif email in _emails(GUESTS_ENV):
            who = email
        else:
            await _refuse(scope, send, 403, f"{APP_NAME} is private, and {email} "
                                            "isn't on its guest list. Ask whoever invited you to add it.")
            return
        with users.acting_as(who):
            await self.app(scope, receive, send)


async def _via_lan(app: Any, scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A request on the home-Wi-Fi port: only a paired device's token counts.

    The Tailscale header, the loopback rule and Cloudflare tokens mean nothing
    here. ``/pair`` swaps a one-time code for a token; see :mod:`stompxl.lan`.
    """
    headers = {name.decode("latin-1").lower(): value.decode("latin-1")
               for name, value in scope.get("headers") or []}
    path = scope.get("path") or ""
    if scope["type"] == "http" and path in OPEN_PATHS:
        await app(scope, receive, send)
        return
    if scope["type"] == "http" and path == "/pair":
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        code = (query.get("code") or [""])[0]
        token = await to_thread.run_sync(
            lan.redeem, code, lan.device_name(headers.get("user-agent", ""))) if code else None
        if token is None:
            await _refuse_phone(scope, send, headers, 403,
                                "That pairing code is wrong, used or expired. Make a new one on "
                                "the computer: More > Phones on this Wi-Fi > Pair a phone.")
            return
        cookie = (f"{lan.COOKIE}={token}; Max-Age={lan.COOKIE_SECONDS}; Path=/; "
                  "HttpOnly; SameSite=Lax")
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", b"/"), (b"set-cookie", cookie.encode()),
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
        return
    device = await to_thread.run_sync(lan.device_for, _cookie(headers, lan.COOKIE))
    if device is None:
        await _refuse_phone(scope, send, headers, 401,
                            f"This phone isn't paired with {APP_NAME} yet. On the computer, open "
                            "More > Phones on this Wi-Fi > Pair a phone, then scan the code with "
                            "this phone's camera or type it below.")
        return
    with users.acting_as(users.OWNER), lan.via_phone():
        await app(scope, receive, send)


_PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#111316">
<title>Pair this phone</title>
<style>
body {{ margin: 0; padding: 24px 16px; background: #111316; color: #e8e8ea;
  font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 420px; margin: 0 auto; }}
h1 {{ font-size: 1.35rem; margin: 0 0 .75rem; }}
p {{ color: #b9bcc4; }}
form {{ display: flex; gap: .5rem; margin: 1.25rem 0; }}
input {{ flex: 1; min-width: 0; min-height: 48px; padding: 0 .8rem; border-radius: 10px; border: 1px solid #3a3e47;
  background: #1b1e23; color: #fff; font-size: 1.2rem; letter-spacing: .12em; text-transform: uppercase; }}
button {{ min-height: 48px; padding: 0 1.1rem; border: 0; border-radius: 10px; background: #e0a526; color: #111;
  font-weight: 600; font-size: 1rem; }}
.small {{ font-size: .88rem; }}
</style></head>
<body><main>
<h1>Pair this phone</h1>
<p>{message}</p>
<form action="/pair" method="get">
  <input name="code" placeholder="ABCD-EFGH" autocomplete="one-time-code" autocapitalize="characters"
    autocorrect="off" spellcheck="false" aria-label="Pairing code" required>
  <button>Pair</button>
</form>
<p class="small">Added the app to your Home Screen on an iPhone or iPad? The Home Screen app keeps its own
sign-in, separate from Safari, so pair it here once by typing a fresh code.</p>
</main></body></html>
"""


async def _refuse_phone(scope: dict[str, Any], send: Any, headers: dict[str, str],
                        status: int, message: str) -> None:
    """Refuse a request on the Wi-Fi port; a browser page gets a code box to pair with."""
    if scope["type"] != "http" or "text/html" not in headers.get("accept", ""):
        await _refuse(scope, send, status, message)
        return
    body = _PAIR_PAGE.format(message=html.escape(message)).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"cache-control", b"no-store"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _refuse(scope: dict[str, Any], send: Any, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = message.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _direct_local(scope: dict[str, Any], headers: dict[str, str]) -> bool:
    """A browser on this Mac talking to the loopback port, not via a proxy."""
    client = (scope.get("client") or ("", 0))[0]
    host = headers.get("host", "")
    if host.startswith("["):  # [::1]:8780
        hostname = host[1:].split("]", 1)[0]
    else:
        hostname = host.split(":", 1)[0]
    return (client in LOOPBACK and hostname in LOOPBACK
            and "x-forwarded-for" not in headers and HEADER not in headers)
