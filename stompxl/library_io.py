"""Export and import a whole library as one zip: backup, and moving to a new computer.

The zip holds:

- ``library.sqlite3``: songs, every version, guitars, sets, tone notes and picks;
- ``settings.json``: the player's choices, with every key and token left out;
- ``NAM/``: the capture folder (``.nam`` files and their converted IRs);
- ``backups/``: the pedal slots saved before each push;
- ``manifest.json``: what made the file, and when.

An import replaces the library and adds the files. Keys stay as they are on
the receiving computer, and the library being replaced is exported first, so
an import can always be undone by importing that file.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import APP_FULL_NAME, config, namlib

FORMAT = 1

#: Never written to an export, never taken from an import.
SECRET_SETTINGS = frozenset({
    "anthropic_api_key", "diagnostics_token", "tone3000_key", "tone3000_tokens",
})
#: About the computer that made the export (paths, first-run setup): not for another one.
LOCAL_SETTINGS = frozenset({"nam_folder", "setup_done", "lan_enabled", "lan_port"})

#: The tables a file must have to count as a library.
_REQUIRED_TABLES = {"songs", "versions", "guitars"}

#: Biggest import accepted, so a wrong file can't fill the disk.
MAX_IMPORT_BYTES = 2 * 1024**3


class ImportRefused(ValueError):
    """The file isn't a library this app can take; nothing was changed."""


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))


def export_zip(lib: Any, dest: Path) -> dict[str, Any]:
    """Write the library of whoever is asking to ``dest``; return the manifest."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "library.sqlite3"
        lib.snapshot(db)
        with contextlib.closing(sqlite3.connect(db)) as c:
            songs = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            versions = c.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        settings = {k: v for k, v in config.read_settings().items()
                    if k not in SECRET_SETTINGS and k not in LOCAL_SETTINGS}
        manifest = {
            "app": APP_FULL_NAME, "format": FORMAT, "exported_at": time.time(),
            "schema": lib.schema_version(), "songs": songs, "versions": versions,
        }
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.write(db, "library.sqlite3")
            z.writestr("settings.json", json.dumps(settings, indent=2))
            for name, root in (("NAM", namlib.folder()), ("backups", config.backups_dir())):
                for path in _files_under(root):
                    z.write(path, f"{name}/{path.relative_to(root).as_posix()}")
    return manifest


def _safe_member(name: str, prefix: str) -> PurePosixPath | None:
    """The path below ``prefix`` that a zip entry may be written to, or None."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        return None
    if len(path.parts) < 2 or path.parts[0] != prefix:
        return None
    rest = PurePosixPath(*path.parts[1:])
    if any(part.startswith(".") for part in rest.parts):
        return None
    return rest


def read_manifest(z: zipfile.ZipFile) -> dict[str, Any]:
    try:
        manifest = json.loads(z.read("manifest.json"))
    except (KeyError, ValueError) as exc:
        raise ImportRefused("That isn't a library export (it has no manifest).") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ImportRefused("That library export is from a version of the app this one can't read.")
    return manifest


def import_zip(lib: Any, source: Path, *, before_dir: Path | None = None) -> dict[str, Any]:
    """Replace the library of whoever is asking with the export at ``source``.

    Checks everything before changing anything; then saves the current library
    to ``before_dir`` (default: the data folder) and swaps the new one in.
    """
    if source.stat().st_size > MAX_IMPORT_BYTES:
        raise ImportRefused("That file is too big to be a library export.")
    try:
        z = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise ImportRefused("That isn't a library export (not a zip file).") from exc
    with z, tempfile.TemporaryDirectory() as tmp:
        manifest = read_manifest(z)
        db = Path(tmp) / "library.sqlite3"
        try:
            db.write_bytes(z.read("library.sqlite3"))
        except KeyError as exc:
            raise ImportRefused("That library export has no library in it.") from exc
        try:
            with contextlib.closing(sqlite3.connect(db)) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ImportRefused("The library in that file is damaged.")
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                newer = c.execute("PRAGMA user_version").fetchone()[0] > lib.schema_version()
        except sqlite3.DatabaseError as exc:
            raise ImportRefused("The library in that file can't be read.") from exc
        if not tables >= _REQUIRED_TABLES:
            raise ImportRefused("That file doesn't hold a song library.")
        if newer:
            raise ImportRefused("That library comes from a newer version of the app. Update this one first.")
        try:
            settings = json.loads(z.read("settings.json"))
        except (KeyError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}

        # Everything checked: keep what's here, then replace it.
        before = (before_dir or config.data_dir()) / time.strftime("before-import-%Y-%m-%d-%H%M%S.zip")
        export_zip(lib, before)
        lib.restore(db)
        config.write_settings({k: v for k, v in settings.items()
                               if k not in SECRET_SETTINGS and k not in LOCAL_SETTINGS})
        files = 0
        for prefix, root in (("NAM", namlib.folder()), ("backups", config.backups_dir())):
            for info in z.infolist():
                rest = None if info.is_dir() else _safe_member(info.filename, prefix)
                if rest is None:
                    continue
                target = root.joinpath(*rest.parts)
                if prefix == "backups" and target.exists():
                    continue  # a pedal backup is never overwritten
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
                files += 1
    return {"songs": manifest.get("songs"), "versions": manifest.get("versions"),
            "files": files, "saved_before": str(before)}
