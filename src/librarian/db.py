"""SQLite schema. One file holds everything (books, pages, lines, FTS5, vectors)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every file seen under the sources root. Several files may share one sha256.
CREATE TABLE IF NOT EXISTS files (
    path      TEXT PRIMARY KEY,      -- relative to sources root, forward slashes
    sha256    TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    ext       TEXT NOT NULL,
    seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);

-- One row per distinct content (deduplicated by sha256).
CREATE TABLE IF NOT EXISTS books (
    sha256        TEXT PRIMARY KEY,
    path          TEXT NOT NULL,     -- canonical (first by sort order) file
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    title         TEXT,
    author        TEXT,
    category      TEXT,
    pdf_pages     INTEGER,
    has_text      INTEGER,           -- 1 if pdftotext finds a text layer on sampled pages
    seed_markdown TEXT,              -- relative to legacy root: library/markdown/LIB-<sha16>.md
    seed_objects  TEXT,              -- relative to legacy root: objects/pdf/<sha256>
    status        TEXT NOT NULL,     -- see inventory.STATUSES
    reason        TEXT,
    updated_at    TEXT NOT NULL
);

-- Filled by the extract stage (phase 2).
CREATE TABLE IF NOT EXISTS pages (
    sha256       TEXT NOT NULL REFERENCES books(sha256),
    pdf_page     INTEGER NOT NULL,   -- 1-based
    printed_page TEXT,               -- 'xiv', '17', NULL if unknown
    method       TEXT,               -- textlayer | llm-ocr:codex | legacy (older rows may say nougat / llm-ocr:openai)
    quality      REAL,
    line_count   INTEGER,
    PRIMARY KEY (sha256, pdf_page)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Several processes share the index (OCR batch, intake check, audit): wait for a writer instead
    # of failing after sqlite's default 5 s.
    con = sqlite3.connect(path, timeout=120)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # Write the schema version only when it changes, and commit at once: Python's sqlite3 otherwise
    # leaves this INSERT in an open transaction, and the connection then holds the write lock
    # until it commits, blocking every other process (the OCR batch's own children included).
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row or row[0] != str(SCHEMA_VERSION):
        con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    con.commit()
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
