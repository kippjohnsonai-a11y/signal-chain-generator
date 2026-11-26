"""The library: guitars, rig profiles, standing instructions, songs and versions.

Plain sqlite3. Every version is immutable once written; a tweak or a fork adds a
new row whose ``parent_id`` points at the version it came from, so the whole
history of a song is a tree you can step back through.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config, quick_picks, xl

SCHEMA = """
CREATE TABLE IF NOT EXISTS guitars (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pickups TEXT NOT NULL,
    pickup_notes TEXT NOT NULL DEFAULT '',
    tuning TEXT NOT NULL DEFAULT 'E standard',
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rigs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('full', 'front', '4cm', 'nam_pedal', 'nam_usb')),
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY,
    guitar_id INTEGER REFERENCES guitars(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    favorite_version_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES versions(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    guitar_id INTEGER REFERENCES guitars(id) ON DELETE SET NULL,
    rig_id INTEGER REFERENCES rigs(id) ON DELETE SET NULL,
    options TEXT NOT NULL,
    brief TEXT NOT NULL,
    chain TEXT NOT NULL,
    preset TEXT NOT NULL,
    feedback TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '[]',
    pushed_slot INTEGER,
    pushed_at REAL,
    user_notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_song ON versions(song_id);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- A set list: a folder's songs in play order. A song can be in several sets.
CREATE TABLE IF NOT EXISTS set_songs (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (folder_id, song_id)
);
CREATE TABLE IF NOT EXISTS version_tags (
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, tag)
);
CREATE INDEX IF NOT EXISTS version_tags_tag ON version_tags(tag);
CREATE TABLE IF NOT EXISTS gear_notes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('nam', 'model', 'category')),
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quick_picks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('category', 'model')),
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    changes TEXT NOT NULL,
    match TEXT NOT NULL DEFAULT '',
    position REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS explanations (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_JSON_COLUMNS = {"options", "brief", "chain", "preset", "warnings"}

#: Tags the library sets itself, from what a version's chain uses.
NAM_TAG = "NAM"
CAPO_TAG = "Poly Capo"
TAG_LIMIT = 32


def auto_tags(chain: dict[str, Any], rig_kind: str | None) -> list[str]:
    """The tags a version gets from its chain: NAM when a NAM capture is its
    amp (on the pedal or the computer), Poly Capo when it has that block."""
    blocks = [b for b in (chain or {}).get("blocks") or [] if isinstance(b, dict)]
    tags = []
    if (chain or {}).get("nam") or rig_kind in xl.NAM_RIGS or any(b.get("nam") for b in blocks):
        tags.append(NAM_TAG)
    if any(b.get("model") == xl.CAPO_MODEL for b in blocks):
        tags.append(CAPO_TAG)
    return tags


def clean_tag(tag: str) -> str:
    return " ".join(str(tag).split())[:TAG_LIMIT]

DEFAULT_RIGS = (
    ("Headphones / FRFR / PA", "full", "Full amp and cab modelling."),
    ("Front of amp", "front", "Into a real amp's input: no amp or cab models."),
    ("4-cable method", "4cm", "Drives before the amp's preamp, time effects in its loop."),
    ("NAM on pedal (custom firmware)", "nam_pedal",
     "A NAM A2 Lite capture from an IR slot is the amp; needs the NAM firmware."),
    ("NAM on computer (USB)", "nam_usb",
     "NAM and a cab IR run on the computer between the pedal's pre- and post-amp effects."),
)


class Library:
    def __init__(self, path: Path | None = None):
        self.path = path or config.db_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._upgrade()

    def _upgrade(self) -> None:
        """Bring the open library up to this version's schema, adding what it lacks."""
        self._conn.executescript(SCHEMA)
        # Libraries made before the player's own notes existed.
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(versions)")}
        if "user_notes" not in columns:
            self._conn.execute("ALTER TABLE versions ADD COLUMN user_notes TEXT NOT NULL DEFAULT ''")
        song_columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(songs)")}
        if "folder_id" not in song_columns:
            self._conn.execute("ALTER TABLE songs ADD COLUMN folder_id INTEGER"
                               " REFERENCES folders(id) ON DELETE SET NULL")
        # Rehearsal navigation: starred songs, set-list order, recently opened.
        for column, kind in (("starred", "INTEGER NOT NULL DEFAULT 0"),
                             ("set_position", "INTEGER"), ("opened_at", "REAL")):
            if column not in song_columns:
                self._conn.execute(f"ALTER TABLE songs ADD COLUMN {column} {kind}")
        self._allow_new_rig_kinds()
        kinds = {r["kind"] for r in self._conn.execute("SELECT kind FROM rigs")}
        for name, kind, notes in DEFAULT_RIGS:
            # A new library gets every default rig; an older one gets the NAM
            # rigs, added since it was made.
            if not kinds or (kind in ("nam_pedal", "nam_usb") and kind not in kinds):
                self._conn.execute(
                    "INSERT INTO rigs (name, kind, notes, created_at) VALUES (?, ?, ?, ?)",
                    (name, kind, notes, time.time()),
                )
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Libraries made before tags: tag what's already there.
            rigs = {r["id"]: r["kind"] for r in self._conn.execute("SELECT id, kind FROM rigs")}
            for row in self._conn.execute("SELECT id, rig_id, chain FROM versions").fetchall():
                self._set_auto_tags(self._conn, row["id"], json.loads(row["chain"]),
                                    rigs.get(row["rig_id"]))
            self._conn.execute("PRAGMA user_version = 1")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            # The starting quick picks, once: deleting them later sticks.
            self._insert_picks(self._conn, quick_picks.default_picks())
            self._conn.execute("PRAGMA user_version = 2")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            # Sets: each folder's songs become that set, in their set-list order.
            rows = self._conn.execute(
                "SELECT id, folder_id FROM songs WHERE folder_id IS NOT NULL"
                " ORDER BY folder_id, set_position IS NULL, set_position, id").fetchall()
            position: dict[int, int] = {}
            for r in rows:
                position[r["folder_id"]] = position.get(r["folder_id"], 0) + 1
                self._conn.execute("INSERT OR IGNORE INTO set_songs (folder_id, song_id, position)"
                                   " VALUES (?, ?, ?)", (r["folder_id"], r["id"], position[r["folder_id"]]))
            self._conn.execute("PRAGMA user_version = 3")
        self._conn.commit()

    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def snapshot(self, dest: Path) -> None:
        """A consistent copy of the whole library at ``dest``, safe while it is in use."""
        with self._lock:
            target = sqlite3.connect(dest)
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def restore(self, source: Path) -> None:
        """Replace everything in this library with the library file at ``source``.

        The copy goes into the open connection, so nothing holding this
        library needs reopening; an older library is then upgraded in place.
        """
        with self._lock:
            src = sqlite3.connect(source)
            try:
                src.backup(self._conn)
            finally:
                src.close()
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._upgrade()

    @staticmethod
    def _set_auto_tags(c: sqlite3.Connection, version_id: int, chain: dict[str, Any],
                       rig_kind: str | None) -> None:
        c.execute("DELETE FROM version_tags WHERE version_id = ? AND auto = 1", (version_id,))
        # A tag the player added by hand stays theirs.
        c.executemany("INSERT OR IGNORE INTO version_tags (version_id, tag, auto) VALUES (?, ?, 1)",
                      [(version_id, t) for t in auto_tags(chain, rig_kind)])

    def _allow_new_rig_kinds(self) -> None:
        """Rebuild an older library's rigs table, whose CHECK lists fewer kinds."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rigs'").fetchone()
        if row is None or "nam_usb" in row["sql"]:
            return
        # legacy_alter_table keeps the versions table's foreign key naming
        # "rigs" through the rename, instead of following it to rigs_old.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            self._conn.executescript(
                "BEGIN;"
                "ALTER TABLE rigs RENAME TO rigs_old;"
                + SCHEMA.split("CREATE TABLE IF NOT EXISTS rigs", 1)[1].split(";", 1)[0]
                .join(["CREATE TABLE rigs", ";"])
                + "INSERT INTO rigs SELECT * FROM rigs_old;"
                "DROP TABLE rigs_old;"
                "COMMIT;"
            )
        finally:
            self._conn.execute("PRAGMA legacy_alter_table = OFF")
            self._conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for key in _JSON_COLUMNS & out.keys():
            out[key] = json.loads(out[key])
        return out

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self._conn.execute(sql, args).fetchone())

    # Guitars ---------------------------------------------------------------

    def guitars(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM guitars ORDER BY name")

    def guitar(self, guitar_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM guitars WHERE id = ?", (guitar_id,))

    def add_guitar(self, name: str, pickups: str, pickup_notes: str = "",
                   tuning: str = "E standard", notes: str = "") -> int:
        with self._tx() as c:
            return c.execute(
                "INSERT INTO guitars (name, pickups, pickup_notes, tuning, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, pickups, pickup_notes, tuning, notes, time.time()),
            ).lastrowid

    def update_guitar(self, guitar_id: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items()
                   if k in ("name", "pickups", "pickup_notes", "tuning", "notes") and v is not None}
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._tx() as c:
            c.execute(f"UPDATE guitars SET {sets} WHERE id = ?", (*allowed.values(), guitar_id))

    def delete_guitar(self, guitar_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM guitars WHERE id = ?", (guitar_id,))

    # Rigs ------------------------------------------------------------------

    def rigs(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM rigs ORDER BY id")

    def rig(self, rig_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM rigs WHERE id = ?", (rig_id,))

    # Standing instructions -------------------------------------------------

    def preferences(self, guitar_id: int | None = None, *, active_only: bool = False
                    ) -> list[dict[str, Any]]:
        """Global instructions plus, when a guitar is given, that guitar's own."""
        sql = "SELECT * FROM preferences WHERE (guitar_id IS NULL"
        args: tuple = ()
        if guitar_id is not None:
            sql += " OR guitar_id = ?"
            args = (guitar_id,)
        sql += ")"
        if active_only:
            sql += " AND active = 1"
        return self._all(sql + " ORDER BY created_at", args)

    def all_preferences(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM preferences ORDER BY created_at")

    def add_preference(self, text: str, guitar_id: int | None = None) -> int:
        with self._tx() as c:
            return c.execute(
                "INSERT INTO preferences (guitar_id, text, created_at) VALUES (?, ?, ?)",
                (guitar_id, text, time.time()),
            ).lastrowid

    def set_preference_active(self, pref_id: int, active: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE preferences SET active = ? WHERE id = ?", (int(active), pref_id))

    def update_preference(self, pref_id: int, fields: dict[str, Any]) -> None:
        """Change any of ``text``, ``guitar_id`` (None = every guitar) and ``active``."""
        allowed = {k: v for k, v in fields.items() if k in ("text", "guitar_id", "active")}
        if "active" in allowed:
            allowed["active"] = int(bool(allowed["active"]))
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._tx() as c:
            c.execute(f"UPDATE preferences SET {sets} WHERE id = ?", (*allowed.values(), pref_id))

    def delete_preference(self, pref_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM preferences WHERE id = ?", (pref_id,))

    # Gear notes: default behaviour for one NAM capture, model or category ----

    def gear_notes(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM gear_notes" + (" WHERE active = 1" if active_only else "")
        return self._all(sql + " ORDER BY kind, name COLLATE NOCASE, created_at")

    def gear_note(self, note_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM gear_notes WHERE id = ?", (note_id,))

    def add_gear_note(self, kind: str, name: str, text: str) -> int:
        with self._tx() as c:
            return c.execute(
                "INSERT INTO gear_notes (kind, name, text, created_at) VALUES (?, ?, ?, ?)",
                (kind, name, text, time.time()),
            ).lastrowid

    def update_gear_note(self, note_id: int, fields: dict[str, Any]) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("kind", "name", "text", "active")}
        if "active" in allowed:
            allowed["active"] = int(bool(allowed["active"]))
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._tx() as c:
            c.execute(f"UPDATE gear_notes SET {sets} WHERE id = ?", (*allowed.values(), note_id))

    def delete_gear_note(self, note_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM gear_notes WHERE id = ?", (note_id,))

    # Quick picks: one-tap moves in the pedal close-up -----------------------

    @staticmethod
    def _insert_picks(c: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
        now = time.time()
        c.executemany(
            "INSERT INTO quick_picks (kind, name, label, changes, match, position, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r["kind"], r["name"], r["label"], json.dumps(r["changes"]), r.get("match", ""),
              r.get("position", 0), now) for r in rows])

    def quick_picks(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM quick_picks ORDER BY kind DESC, name COLLATE NOCASE,"
                         " position, id")
        for row in rows:
            row["changes"] = json.loads(row["changes"])
        return rows

    def quick_pick(self, pick_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM quick_picks WHERE id = ?", (pick_id,))
        if row is not None:
            row["changes"] = json.loads(row["changes"])
        return row

    def add_quick_pick(self, kind: str, name: str, label: str, changes: list[dict[str, Any]],
                       match: str = "") -> int:
        with self._tx() as c:
            last = c.execute("SELECT MAX(position) FROM quick_picks WHERE kind = ? AND name = ?",
                             (kind, name)).fetchone()[0]
            return c.execute(
                "INSERT INTO quick_picks (kind, name, label, changes, match, position, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, name, label, json.dumps(changes), match,
                 (last if last is not None else -1) + 1, time.time())).lastrowid

    def update_quick_pick(self, pick_id: int, fields: dict[str, Any]) -> None:
        allowed = {k: v for k, v in fields.items()
                   if k in ("kind", "name", "label", "changes", "match", "position")}
        if "changes" in allowed:
            allowed["changes"] = json.dumps(allowed["changes"])
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._tx() as c:
            c.execute(f"UPDATE quick_picks SET {sets} WHERE id = ?", (*allowed.values(), pick_id))

    def delete_quick_pick(self, pick_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM quick_picks WHERE id = ?", (pick_id,))

    def restore_quick_picks(self, category: str | None = None) -> None:
        """Put the starting picks back (for one kind, or all), keeping the player's own."""
        rows = quick_picks.default_picks(category)
        with self._tx() as c:
            for r in rows:
                c.execute("DELETE FROM quick_picks WHERE kind = 'category' AND name = ? AND label = ?",
                          (r["name"], r["label"]))
            self._insert_picks(c, rows)

    # Songs and versions ----------------------------------------------------

    def songs(self) -> list[dict[str, Any]]:
        """Every song, newest first, with each version's tags in ``tag_sets``
        (one list per version) so a search can ask for one version with all
        the tags it names."""
        songs = self._all(
            "SELECT s.*, COUNT(v.id) AS version_count, MAX(v.created_at) AS updated_at"
            " FROM songs s LEFT JOIN versions v ON v.song_id = s.id"
            " GROUP BY s.id ORDER BY updated_at DESC"
        )
        sets: dict[int, dict[int, list[str]]] = {}
        for r in self._all("SELECT v.song_id, t.version_id, t.tag FROM version_tags t"
                           " JOIN versions v ON v.id = t.version_id ORDER BY t.tag"):
            sets.setdefault(r["song_id"], {}).setdefault(r["version_id"], []).append(r["tag"])
        # The version a song opens on (its favorite, else the newest), for the
        # list's guitar and pedal slot.
        latest: dict[int, dict[str, Any]] = {}
        for r in self._all("SELECT id, song_id, guitar_id, pushed_slot FROM versions ORDER BY created_at"):
            latest[r["song_id"]] = r
        by_id = {r["id"]: r for r in latest.values()}
        favs = [s["favorite_version_id"] for s in songs if s.get("favorite_version_id")]
        if favs:
            marks = ",".join("?" * len(favs))
            for r in self._all(f"SELECT id, song_id, guitar_id, pushed_slot FROM versions WHERE id IN ({marks})", favs):
                by_id[r["id"]] = r
        for song in songs:
            v = by_id.get(song.get("favorite_version_id")) or latest.get(song["id"])
            song["open_version_id"] = v["id"] if v else None
            song["guitar_id"] = v["guitar_id"] if v else None
            song["pushed_slot"] = v["pushed_slot"] if v else None
        for song in songs:
            song["tag_sets"] = list(sets.get(song["id"], {}).values())
            song["tags"] = sorted({t for ts in song["tag_sets"] for t in ts}, key=str.lower)
        return songs

    def song(self, song_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM songs WHERE id = ?", (song_id,))

    def add_song(self, query: str, title: str, artist: str) -> int:
        with self._tx() as c:
            return c.execute(
                "INSERT INTO songs (query, title, artist, created_at) VALUES (?, ?, ?, ?)",
                (query, title, artist, time.time()),
            ).lastrowid

    def delete_song(self, song_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM songs WHERE id = ?", (song_id,))

    def set_song_folder(self, song_id: int, folder_id: int | None) -> None:
        """Move a song into a folder, at the end of its set list."""
        with self._tx() as c:
            row = c.execute("SELECT folder_id FROM songs WHERE id = ?", (song_id,)).fetchone()
            if row is not None and row["folder_id"] == folder_id:
                return
            last = c.execute("SELECT MAX(set_position) FROM songs WHERE folder_id = ?",
                             (folder_id,)).fetchone()[0]
            position = None if folder_id is None else (last or 0) + 1
            c.execute("UPDATE songs SET folder_id = ?, set_position = ? WHERE id = ?",
                      (folder_id, position, song_id))

    def set_song_order(self, folder_id: int, song_ids: list[int]) -> None:
        """Put a folder's songs in play order; songs not listed keep their place after them."""
        with self._tx() as c:
            inside = [r["id"] for r in c.execute(
                "SELECT id FROM songs WHERE folder_id = ?"
                " ORDER BY set_position IS NULL, set_position, id", (folder_id,))]
            order = [i for i in song_ids if i in inside] + [i for i in inside if i not in song_ids]
            c.executemany("UPDATE songs SET set_position = ? WHERE id = ?",
                          [(n + 1, i) for n, i in enumerate(order)])

    # Sets (set lists) ------------------------------------------------------
    # The folders table names the sets; set_songs holds each one's songs in play order.

    def set_songs(self) -> dict[int, list[int]]:
        """Each set's song ids, in play order."""
        out: dict[int, list[int]] = {f["id"]: [] for f in self.folders()}
        for r in self._all("SELECT folder_id, song_id FROM set_songs ORDER BY folder_id, position"):
            out.setdefault(r["folder_id"], []).append(r["song_id"])
        return out

    def replace_set(self, folder_id: int, song_ids: list[int]) -> None:
        """A set's songs, in play order; songs left out leave the set."""
        known = {r["id"] for r in self._all("SELECT id FROM songs")}
        order = list(dict.fromkeys(i for i in song_ids if i in known))
        with self._tx() as c:
            c.execute("DELETE FROM set_songs WHERE folder_id = ?", (folder_id,))
            c.executemany("INSERT INTO set_songs (folder_id, song_id, position) VALUES (?, ?, ?)",
                          [(folder_id, i, n + 1) for n, i in enumerate(order)])

    def set_song_sets(self, song_id: int, folder_ids: list[int]) -> None:
        """Which sets a song is in: added sets get it at the end, unticked ones lose it."""
        with self._tx() as c:
            current = {r["folder_id"] for r in c.execute(
                "SELECT folder_id FROM set_songs WHERE song_id = ?", (song_id,))}
            wanted = set(folder_ids)
            for fid in current - wanted:
                c.execute("DELETE FROM set_songs WHERE folder_id = ? AND song_id = ?", (fid, song_id))
            for fid in wanted - current:
                last = c.execute("SELECT MAX(position) FROM set_songs WHERE folder_id = ?",
                                 (fid,)).fetchone()[0]
                c.execute("INSERT INTO set_songs (folder_id, song_id, position) VALUES (?, ?, ?)",
                          (fid, song_id, (last or 0) + 1))

    def set_starred(self, song_id: int, starred: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE songs SET starred = ? WHERE id = ?", (int(starred), song_id))

    def mark_opened(self, song_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE songs SET opened_at = ? WHERE id = ?", (time.time(), song_id))

    def set_favorite(self, song_id: int, version_id: int | None) -> None:
        with self._tx() as c:
            c.execute("UPDATE songs SET favorite_version_id = ? WHERE id = ?",
                      (version_id, song_id))

    def versions(self, song_id: int) -> list[dict[str, Any]]:
        versions = self._all(
            "SELECT id, song_id, parent_id, kind, label, guitar_id, rig_id, options, feedback,"
            " notes, pushed_slot, pushed_at, created_at FROM versions"
            " WHERE song_id = ? ORDER BY created_at",
            (song_id,),
        )
        tags = self._tags_of([v["id"] for v in versions])
        for v in versions:
            v["tags"] = tags.get(v["id"], [])
        return versions

    def version(self, version_id: int) -> dict[str, Any] | None:
        record = self._one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if record:
            record["tags"] = self._tags_of([version_id]).get(version_id, [])
        return record

    def _tags_of(self, version_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not version_ids:
            return {}
        marks = ", ".join("?" * len(version_ids))
        out: dict[int, list[dict[str, Any]]] = {}
        for r in self._all(f"SELECT version_id, tag, auto FROM version_tags"
                           f" WHERE version_id IN ({marks}) ORDER BY auto DESC, tag COLLATE NOCASE",
                           tuple(version_ids)):
            out.setdefault(r["version_id"], []).append({"tag": r["tag"], "auto": bool(r["auto"])})
        return out

    def lineage_feedback(self, version_id: int) -> list[str]:
        """Feedback that led to this version, oldest first, following parents."""
        out: list[str] = []
        current = self.version(version_id)
        seen: set[int] = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            if current["feedback"]:
                out.append(current["feedback"])
            current = self.version(current["parent_id"]) if current["parent_id"] else None
        return list(reversed(out))

    def add_version(self, *, song_id: int, parent_id: int | None, kind: str, label: str,
                    guitar_id: int | None, rig_id: int | None, options: dict[str, Any],
                    brief: dict[str, Any], chain: dict[str, Any], preset: dict[str, Any],
                    feedback: str = "", notes: str = "", warnings: list[str] | None = None,
                    user_notes: str | None = None) -> int:
        """``user_notes`` and the player's own tags default to the parent's, so
        they follow every tweak, fork, edit and copy of a version. Tags read
        off the chain (NAM, Poly Capo) are worked out afresh."""
        parent = self.version(parent_id) if parent_id else None
        if user_notes is None:
            user_notes = parent["user_notes"] if parent else ""
        rig = self.rig(rig_id) if rig_id else None
        with self._tx() as c:
            version_id = c.execute(
                "INSERT INTO versions (song_id, parent_id, kind, label, guitar_id, rig_id,"
                " options, brief, chain, preset, feedback, notes, warnings, user_notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (song_id, parent_id, kind, label, guitar_id, rig_id, json.dumps(options),
                 json.dumps(brief), json.dumps(chain), json.dumps(preset), feedback, notes,
                 json.dumps(warnings or []), user_notes, time.time()),
            ).lastrowid
            if parent:
                c.executemany(
                    "INSERT OR IGNORE INTO version_tags (version_id, tag, auto) VALUES (?, ?, 0)",
                    [(version_id, t["tag"]) for t in parent["tags"] if not t["auto"]])
            self._set_auto_tags(c, version_id, chain, rig["kind"] if rig else None)
            return version_id

    def explanation(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT body FROM explanations WHERE key = ?", (key,)).fetchone()
        return json.loads(row["body"]) if row else None

    def save_explanation(self, key: str, body: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO explanations (key, body, created_at) VALUES (?, ?, ?)",
                      (key, json.dumps(body), time.time()))

    def set_user_notes(self, version_id: int, text: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE versions SET user_notes = ? WHERE id = ?", (text, version_id))

    def set_preset_name(self, version_id: int, chain: dict[str, Any], preset: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("UPDATE versions SET chain = ?, preset = ? WHERE id = ?",
                      (json.dumps(chain), json.dumps(preset), version_id))

    def set_brief(self, version_id: int, brief: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("UPDATE versions SET brief = ? WHERE id = ?", (json.dumps(brief), version_id))

    def rename_version(self, version_id: int, label: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE versions SET label = ? WHERE id = ?", (label, version_id))

    def mark_pushed(self, version_id: int, slot: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE versions SET pushed_slot = ?, pushed_at = ? WHERE id = ?",
                      (slot, time.time(), version_id))

    def delete_version(self, version_id: int) -> None:
        self.delete_versions([version_id])

    def delete_versions(self, version_ids: list[int]) -> int:
        """Delete these versions; versions forked from them are kept. A song
        whose favorite goes loses the star. Returns how many were deleted."""
        ids = sorted({int(i) for i in version_ids})
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        with self._tx() as c:
            c.execute(f"UPDATE songs SET favorite_version_id = NULL"
                      f" WHERE favorite_version_id IN ({marks})", ids)
            return c.execute(f"DELETE FROM versions WHERE id IN ({marks})", ids).rowcount

    # Tags ------------------------------------------------------------------

    def tags(self) -> list[dict[str, Any]]:
        """Every tag in the library, with how many versions carry it."""
        return self._all(
            "SELECT tag, MAX(auto) AS auto, COUNT(*) AS count FROM version_tags"
            " GROUP BY tag ORDER BY MAX(auto) DESC, tag COLLATE NOCASE")

    def _spelling(self, c: sqlite3.Connection, tag: str) -> str:
        """A tag as the library already spells it ("nam" -> "NAM")."""
        tag = clean_tag(tag)
        for known in (NAM_TAG, CAPO_TAG):
            if tag.lower() == known.lower():
                return known
        row = c.execute("SELECT tag FROM version_tags WHERE tag = ? COLLATE NOCASE LIMIT 1",
                        (tag,)).fetchone()
        return row["tag"] if row else tag

    def _spellings(self, c: sqlite3.Connection, tags: Any) -> list[str]:
        """Distinct tags, ignoring case, each as the library spells it."""
        out: dict[str, str] = {}
        for tag in tags:
            if clean_tag(tag):
                out.setdefault(clean_tag(tag).lower(), self._spelling(c, tag))
        return list(out.values())

    def set_tags(self, version_id: int, tags: list[str]) -> None:
        """Replace the player's own tags on a version (automatic ones stay)."""
        with self._tx() as c:
            c.execute("DELETE FROM version_tags WHERE version_id = ? AND auto = 0", (version_id,))
            wanted = self._spellings(c, tags)
            c.executemany("INSERT OR IGNORE INTO version_tags (version_id, tag, auto) VALUES (?, ?, 0)",
                          [(version_id, t) for t in wanted])

    def tag_versions(self, version_ids: list[int], add: list[str] = (),
                     remove: list[str] = ()) -> None:
        """Add and remove the player's own tags across several versions."""
        with self._tx() as c:
            adds = self._spellings(c, add)
            drops = {clean_tag(t).lower() for t in remove if clean_tag(t)}
            for vid in {int(i) for i in version_ids}:
                for tag in drops:
                    c.execute("DELETE FROM version_tags WHERE version_id = ? AND auto = 0"
                              " AND lower(tag) = ?", (vid, tag))
                c.executemany(
                    "INSERT OR IGNORE INTO version_tags (version_id, tag, auto)"
                    " SELECT ?, ?, 0 WHERE EXISTS (SELECT 1 FROM versions WHERE id = ?)",
                    [(vid, t, vid) for t in adds])

    # Folders ---------------------------------------------------------------

    def folders(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM folders ORDER BY name COLLATE NOCASE")

    def add_folder(self, name: str) -> int:
        with self._tx() as c:
            return c.execute("INSERT INTO folders (name, created_at) VALUES (?, ?)",
                             (name, time.time())).lastrowid

    def rename_folder(self, folder_id: int, name: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))

    def delete_folder(self, folder_id: int) -> None:
        """Delete a folder; its songs are kept, out of any folder."""
        with self._tx() as c:
            c.execute("UPDATE songs SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
            c.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
