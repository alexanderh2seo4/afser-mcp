import json
import os
import secrets
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from .config import private_write
from .models import KINDS, SourceSnapshot
from .privacy import project


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def search_key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


class Store:
    """Raw source payloads never leave this class through a read API."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "afser.sqlite3"
        if self.path.is_symlink():
            raise ValueError("private_database_symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.lock = threading.RLock()
        salt_path = directory / "projection.key"
        if salt_path.is_symlink():
            raise ValueError("private_key_symlink")
        if not salt_path.exists():
            # Exclusive creation is safe when HTTP and MCP start simultaneously.
            try:
                fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
        salt_path.chmod(0o600)
        self.salt = salt_path.read_bytes()
        if len(self.salt) != 32:
            raise ValueError("invalid_projection_key")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_records (
                    snapshot INTEGER NOT NULL, entity_type TEXT NOT NULL, source_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (snapshot, entity_type, source_id));
                CREATE TABLE IF NOT EXISTS public_records (
                    snapshot INTEGER NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
                    chapter TEXT NOT NULL, active INTEGER NOT NULL, urgent INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY (snapshot, id));
                CREATE INDEX IF NOT EXISTS public_filter ON public_records(snapshot,kind,chapter,active);
                CREATE TABLE IF NOT EXISTS chapters (
                    snapshot INTEGER NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY (snapshot,id));
                CREATE TABLE IF NOT EXISTS places (
                    snapshot INTEGER NOT NULL, id TEXT NOT NULL, city TEXT NOT NULL,
                    search_key TEXT NOT NULL, chapter TEXT, latitude REAL NOT NULL, longitude REAL NOT NULL,
                    postal_code TEXT, PRIMARY KEY (snapshot,id));
            """)

    @contextmanager
    def connect(self, read_snapshot=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        try:
            with db:
                if read_snapshot:
                    db.execute("BEGIN")
                yield db
        finally:
            db.close()

    @staticmethod
    def _active(db) -> int | None:
        row = db.execute("SELECT value FROM meta WHERE key='active_snapshot'").fetchone()
        return int(row[0]) if row else None

    def activate(self, snapshot: SourceSnapshot) -> dict:
        """Publish only after every source page and entity validates successfully."""
        chapter_ids = {chapter.id for chapter in snapshot.chapters}
        if len(chapter_ids) != len(snapshot.chapters):
            raise ValueError("duplicate_chapter")
        if "unassigned" in chapter_ids:
            raise ValueError("reserved_chapter_id")
        counts = {"raw": 0, "active": 0, **{kind: 0 for kind in sorted(KINDS)}}
        timestamp = now()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snap_id = db.execute("INSERT INTO snapshots(created_at) VALUES (?)", (timestamp,)).lastrowid
            for chapter in snapshot.chapters:
                if not chapter.id or len(chapter.id) > 100 or not chapter.name or len(chapter.name) > 120:
                    raise ValueError("invalid_chapter")
                db.execute("INSERT INTO chapters VALUES (?,?,?)", (snap_id, chapter.id, chapter.name))
            for place in snapshot.places:
                if place.chapter_id is not None and place.chapter_id not in chapter_ids:
                    raise ValueError("unknown_place_chapter")
                if not (-90 <= place.latitude <= 90 and -180 <= place.longitude <= 180):
                    raise ValueError("invalid_place_coordinate")
                db.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?)", (snap_id, place.id, place.city, search_key(place.city), place.chapter_id, place.latitude, place.longitude, place.postal_code))
            for raw in snapshot.records:
                if not raw.source_id or not raw.entity_type:
                    raise ValueError("invalid_raw_record")
                # Complete source payload is written to disk without ever being logged.
                db.execute("INSERT INTO raw_records VALUES (?,?,?,?)", (snap_id, raw.entity_type, raw.source_id, json.dumps(raw.payload, ensure_ascii=False)))
                counts["raw"] += 1
                if raw.normalized is not None:
                    record = raw.normalized
                    # Verified open tasks without a chapter remain visible in
                    # explicit All queries. They cannot become a guessed local
                    # assignment or a selectable fabricated chapter.
                    if record.chapter_id not in chapter_ids and record.chapter_id != "unassigned":
                        raise ValueError("unknown_record_chapter")
                    public = project(record, self.salt)
                    db.execute("INSERT INTO public_records VALUES (?,?,?,?,?,?,?)", (snap_id, public["id"], record.kind, record.chapter_id, int(record.active), int(record.urgent), json.dumps(public, ensure_ascii=False)))
                    if record.active:
                        counts["active"] += 1
                        counts[record.kind] += 1
            db.execute("INSERT OR REPLACE INTO meta VALUES ('active_snapshot',?)", (str(snap_id),))
            for table in ("raw_records", "public_records", "chapters", "places", "snapshots"):
                column = "id" if table == "snapshots" else "snapshot"
                db.execute(f"DELETE FROM {table} WHERE {column} <> ?", (snap_id,))
        self.path.chmod(0o600)
        return {"updatedAt": timestamp, "counts": counts}

    def status(self) -> dict:
        with self.connect(read_snapshot=True) as db:
            snapshot = self._active(db)
            row = db.execute("SELECT created_at FROM snapshots WHERE id=?", (snapshot,)).fetchone()
            counts = {kind: 0 for kind in sorted(KINDS)}
            for item in db.execute("SELECT kind,COUNT(*) AS total FROM public_records WHERE snapshot=? AND active=1 GROUP BY kind", (snapshot,)):
                counts[item["kind"]] = item["total"]
            return {"ready": snapshot is not None, "updatedAt": row[0] if row else None, "counts": counts, "privacy": "approximate-locations; no-personal-fields"}

    def chapters(self) -> dict:
        with self.connect(read_snapshot=True) as db:
            snapshot = self._active(db)
            return {"chapters": [dict(row) for row in db.execute("SELECT id,name FROM chapters WHERE snapshot=? ORDER BY name", (snapshot,))]}

    def records(self, kind: str, chapter: str, limit: int | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError("invalid_kind")
        with self.connect(read_snapshot=True) as db:
            snapshot = self._active(db)
            sql = "SELECT body FROM public_records WHERE snapshot=? AND kind=? AND active=1"
            params: list = [snapshot, kind]
            if chapter != "all":
                exists = db.execute("SELECT 1 FROM chapters WHERE snapshot=? AND id=?", (snapshot, chapter)).fetchone()
                if not exists:
                    raise ValueError("unknown_chapter")
                sql += " AND chapter=?"
                params.append(chapter)
            sql += " ORDER BY urgent DESC,id"
            if limit is not None:
                if not 1 <= limit <= 200:
                    raise ValueError("invalid_limit")
                sql += " LIMIT ?"
                params.append(limit)
            items = [json.loads(row[0]) for row in db.execute(sql, params)]
            timestamp = db.execute("SELECT created_at FROM snapshots WHERE id=?", (snapshot,)).fetchone()
            return {"records": items, "updatedAt": timestamp[0] if timestamp else None, "chapter": chapter, "kind": kind}

    def places(self, query: str) -> dict:
        query = query.strip()
        if not 2 <= len(query) <= 80:
            return {"places": []}
        key = search_key(query)
        with self.connect(read_snapshot=True) as db:
            snapshot = self._active(db)
            rows = db.execute("SELECT MIN(p.id) AS id,p.city,p.chapter,AVG(p.latitude) AS latitude,AVG(p.longitude) AS longitude,c.name AS chapter_name FROM places p LEFT JOIN chapters c ON c.snapshot=p.snapshot AND c.id=p.chapter WHERE p.snapshot=? AND (substr(p.search_key,1,?)=? OR substr(p.postal_code,1,?)=?) GROUP BY p.search_key,p.chapter ORDER BY CASE WHEN p.search_key=? THEN 0 ELSE 1 END,p.search_key,c.name LIMIT 12", (snapshot, len(key), key, len(query), query, key))
            return {"places": [{"id": row["id"], "city": row["city"], "label": row["city"] + (" · " + row["chapter_name"] if row["chapter_name"] else ""), "chapterId": row["chapter"], "location": {"lat": round(row["latitude"], 4), "lon": round(row["longitude"], 4)}} for row in rows]}
