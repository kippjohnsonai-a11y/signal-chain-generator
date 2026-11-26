"""A diagnostics bundle the owner can send from the app with one tap.

When a send to the pedal fails, what helps is the code that was running, the
error, and the USB traffic that led up to it. Until now that meant a terminal
on the Mac mini (``sudo grep ... stompxl.log``). Instead the app keeps the last
few failures (in memory and in ``<data dir>/diagnostics/failures.json``, so a
restart doesn't lose them) plus its recent log lines, and the Send diagnostics
button posts them as an issue in the app's own private GitHub repository,
labelled ``diagnostics``, where a Claude session can read them.

Sending needs a fine-grained GitHub token that can only write issues on that
repository: ``STOMPXL_DIAGNOSTICS_TOKEN`` in the env file, or pasted into
Settings. Without one the bundle is still built and shown, to copy by hand.

Everything that leaves is passed through :func:`redact`: email addresses (the
guests'), API keys and tokens, and any secret the app holds, are blanked.
Preset data (frame headers, parameter values) is left in on purpose.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

TOKEN_ENV = "STOMPXL_DIAGNOSTICS_TOKEN"
REPO_ENV = "STOMPXL_DIAGNOSTICS_REPO"
DEFAULT_REPO = "kippjohnsonai-a11y/signal-chain-generator"
LABEL = "diagnostics"
#: GitHub refuses issue bodies over 65,536 characters.
MAX_BODY = 60_000
KEEP_FAILURES = 5
KEEP_LOG_LINES = 300

_lock = threading.Lock()
_failures: collections.deque[dict[str, Any]] = collections.deque(maxlen=KEEP_FAILURES)
_loaded = False


# -- recent log lines ---------------------------------------------------------

class _RingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: collections.deque[str] = collections.deque(maxlen=KEEP_LOG_LINES)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # logging must never raise
            self.lines.append(self.format(record))


_ring = _RingHandler()


def install_log_ring() -> None:
    """Keep the app's recent log lines, for the bundle. Safe to call twice."""
    root = logging.getLogger()
    if _ring not in root.handlers:
        root.addHandler(_ring)


# -- failures -----------------------------------------------------------------

def _failures_path() -> Path:
    path = config.base_dir() / "diagnostics"
    path.mkdir(parents=True, exist_ok=True)
    return path / "failures.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        saved = json.loads(_failures_path().read_text())
    except (OSError, ValueError):
        return
    if isinstance(saved, list):
        _failures.extend(f for f in saved[-KEEP_FAILURES:] if isinstance(f, dict))


def record_failure(what: str, error: BaseException | str, *, frames: str = "",
                   relayed: str = "") -> None:
    """Keep a failed pedal operation with the traffic that led up to it."""
    from . import users

    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "what": what,
        "error": f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error,
        "who": "guest" if users.is_guest() else "owner",
        "frames": frames,
        "relayed": relayed,
    }
    with _lock:
        _load()
        _failures.append(entry)
        try:
            path = _failures_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(_failures), indent=1))
            tmp.replace(path)
        except OSError:
            logging.getLogger(__name__).warning("Could not save the failure for diagnostics")


def failures() -> list[dict[str, Any]]:
    with _lock:
        _load()
        return list(_failures)


# -- redaction ----------------------------------------------------------------

_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"t3k_(?:pub|cs)_[A-Za-z0-9_-]+"),
    re.compile(r"(?i)((?:bearer|authorization:?)\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',]{8,}"),
]


def _secrets() -> list[str]:
    """Secret values the app holds, so they can be blanked wherever they turn up."""
    settings = config._read(config.base_dir() / "settings.json")
    values = [settings.get(k) for k in ("anthropic_api_key", "tone3000_key", "diagnostics_token")]
    values += [os.environ.get(k) for k in os.environ
               if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k)]
    for listed in ("STOMPXL_GUEST_EMAILS", "STOMPXL_OWNER_EMAILS", "STOMPXL_ALLOWED_LOGINS"):
        values += re.split(r"[\s,]+", os.environ.get(listed, ""))
    return sorted({v for v in values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)


#: Personal details a report never needs: emails, pedal serial numbers.
_PERSONAL = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), "[email]"),
    (re.compile(r"(?i)\bserial(\s*(?:number|no\.?)?[\s:#=]*)[A-Za-z0-9-]{4,}"), r"serial\1[hidden]"),
]


def redact(text: str) -> str:
    for secret in _secrets():
        text = text.replace(secret, "[redacted]")
    for pattern in _PATTERNS:
        text = pattern.sub(r"\1[redacted]" if pattern.groups else "[redacted]", text)
    for pattern, replacement in _PERSONAL:
        text = pattern.sub(replacement, text)
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    return text


# -- the bundle ---------------------------------------------------------------

def _pedal_line() -> str:
    from . import pedal

    try:
        s = pedal.status()
    except Exception as exc:  # noqa: BLE001
        return f"status failed: {exc}"
    if not s.connected:
        return f"not connected{f' ({s.error})' if s.error else ''}"
    return f"{s.description}{f' via the page on a {s.where}' if s.where else ' on the server USB'}"


def _recent_backups() -> list[str]:
    try:
        files = sorted(config.base_dir().joinpath("backups").iterdir(),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    except OSError:
        return []
    return [f"{p.name} ({p.stat().st_size} B, "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))})"
            for p in files]


def _fence(text: str) -> str:
    return "```\n" + (text.strip() or "(none)").replace("```", "'''") + "\n```"


def bundle(version: dict[str, Any], *, note: str = "", client: dict[str, Any] | None = None) -> str:
    """The diagnostics as Markdown, already redacted and cut to fit an issue."""
    client = client or {}
    head = [
        "## Running",
        f"- Commit: `{version.get('commit', 'unknown')}` ({version.get('date', '')})",
        f"- Server: Python {platform.python_version()} on {platform.platform()} ({sys.platform})",
        f"- Pedal: {_pedal_line()}",
        f"- Sent: {datetime.now(UTC).isoformat(timespec='seconds')}",
    ]
    if client.get("user_agent"):
        head.append(f"- Browser: {client['user_agent']}")
    if client.get("relay"):
        head.append(f"- This page's USB relay: {json.dumps(client['relay'])}")
    if note.strip():
        head += ["", "## Note", note.strip()]
    if client.get("error"):
        head += ["", "## Error the app showed", _fence(str(client["error"]))]
    if client.get("events"):
        head += ["", "## What this browser showed and sent (oldest first)",
                 _fence("\n".join(client["events"]))]

    sections = []
    recorded = failures()
    if recorded:
        for i, f in enumerate(reversed(recorded), 1):
            part = [f"## Failure {i} of {len(recorded)} (newest first): {f.get('what')}",
                    f"- At: {f.get('at')} · for: {f.get('who')}",
                    f"- Error: `{f.get('error')}`",
                    "", "Last frames on the wire:", _fence(f.get("frames") or "")]
            if f.get("relayed"):
                part += ["", "Last relayed USB calls:", _fence(f["relayed"])]
            sections.append("\n".join(part))
    else:
        sections.append("## Failures\nNo pedal failure recorded since this was added.")
    backups = _recent_backups()
    if backups:
        sections.append("## Newest backups\n" + "\n".join(f"- {b}" for b in backups))

    log_lines = list(_ring.lines)
    text = ""
    # Drop the oldest log lines, then the oldest failures, until it fits.
    while True:
        log = "## Recent log\n" + _fence("\n".join(log_lines))
        text = redact("\n".join(head) + "\n\n" + "\n\n".join([*sections, log]) + "\n")
        if len(text) <= MAX_BODY:
            return text
        if len(log_lines) > 20:
            log_lines = log_lines[len(log_lines) // 3:]
        elif len(sections) > 1:
            sections.pop(len(sections) - 2 if backups else -1)
        else:
            return text[:MAX_BODY - 40] + "\n\n(cut to fit)\n"


def headline(note: str = "", error: str = "") -> str:
    """A title for the issue: what the owner said, else the error, else the newest failure."""
    for text in (note, error):
        line = " ".join(str(text or "").split())
        if line:
            return redact(line if len(line) <= 90 else line[:89] + "...")
    latest = (failures() or [{}])[-1].get("what")
    return f"last failure: {latest}" if latest else "no recorded failure"


# -- sending ------------------------------------------------------------------

def token() -> str | None:
    return (os.environ.get(TOKEN_ENV)
            or config._read(config.base_dir() / "settings.json").get("diagnostics_token") or None)


def repo() -> str:
    return os.environ.get(REPO_ENV) or DEFAULT_REPO


class SendError(RuntimeError):
    pass


def new_issue_url(title: str) -> str:
    """GitHub's new-issue page for :func:`repo`, titled, for reporting without a token.

    The report itself is too long for a link, so the page asks for it to be pasted.
    """
    from urllib.parse import urlencode

    body = ("<!-- Paste the problem report here: the app copied it for you. "
            "Check it first: issues here are public. -->\n\n")
    return f"https://github.com/{repo()}/issues/new?" + urlencode(
        {"title": title, "body": body, "labels": LABEL})


def _post(url: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "stompxl-diagnostics"})
    with urllib.request.urlopen(request, timeout=20) as reply:
        return json.loads(reply.read())


def send(title: str, body: str) -> str:
    """Open an issue with the bundle. Returns its URL."""
    key = token()
    if not key:
        raise SendError("No diagnostics token yet: add one in Settings (see docs/diagnostics.md).")
    url = f"https://api.github.com/repos/{repo()}/issues"
    payload = {"title": title, "body": body, "labels": [LABEL]}
    try:
        try:
            return _post(url, payload, key)["html_url"]
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
            # A token that can't create the label: send it without one.
            payload.pop("labels")
            return _post(url, payload, key)["html_url"]
    except urllib.error.HTTPError as exc:
        reason = {401: "the token was refused", 403: "the token can't write issues there",
                  404: f"the token can't see {repo()}"}.get(exc.code, f"GitHub said {exc.code}")
        raise SendError(f"Couldn't send diagnostics: {reason}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SendError(f"Couldn't reach GitHub: {exc}") from exc
