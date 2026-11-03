"""Phase 2 index: page FTS5 + line-window chunks with embeddings, all in one SQLite file.

Vectors live in a sqlite-vec `vec0` table when the extension is installed, otherwise in a
BLOB table searched with numpy (fine for the priority books, slow for the whole library).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from .config import Config
from .db import now, set_meta
from .embed import Embedder
from .log import Timer, log
from .pagemap import read_pages

CHUNK_LINES = 12
CHUNK_STRIDE = 8
MIN_CHUNK_CHARS = 80

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    text, sha256 UNINDEXED, pdf_page UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL,
    pdf_page   INTEGER NOT NULL,
    line_start INTEGER NOT NULL,   -- 1-based, inclusive, lines of the page file
    line_end   INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_book ON chunks(sha256, pdf_page);
CREATE TABLE IF NOT EXISTS index_state (
    sha256      TEXT PRIMARY KEY,
    extracted_at TEXT NOT NULL,
    embed_spec  TEXT NOT NULL,
    chunks      INTEGER NOT NULL,
    indexed_at  TEXT NOT NULL
);
"""


def has_vec(con: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore[import-not-found]
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return True
    except Exception:  # noqa: BLE001
        return False


class VectorStore:
    def __init__(self, con: sqlite3.Connection, dim: int, spec: str, create: bool = True):
        self.con, self.dim = con, dim
        self.vec = has_vec(con)
        old = con.execute("SELECT value FROM meta WHERE key='embed_spec'").fetchone()
        if create and old and old[0] != spec:
            raise SystemExit(f"index was built with {old[0]!r}, not {spec!r}: rerun with --reembed")
        if create:
            if self.vec:
                con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{dim}])")
            else:
                log.warning("sqlite-vec not available: storing vectors as BLOBs (numpy search)")
                con.execute("CREATE TABLE IF NOT EXISTS chunks_blob (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
            set_meta(con, "embed_spec", spec)
            set_meta(con, "embed_dim", str(dim))
            set_meta(con, "vector_backend", "sqlite-vec" if self.vec else "blob")

    def add(self, ids: list[int], m: np.ndarray) -> None:
        table = "chunks_vec(rowid, embedding)" if self.vec else "chunks_blob(id, v)"
        self.con.executemany(f"INSERT INTO {table} VALUES(?, ?)",
                             [(i, v.astype(np.float32).tobytes()) for i, v in zip(ids, m)])

    def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        table, col = ("chunks_vec", "rowid") if self.vec else ("chunks_blob", "id")
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            self.con.execute(f"DELETE FROM {table} WHERE {col} IN ({','.join('?' * len(part))})", part)

    def knn(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        q = q.astype(np.float32)
        if self.vec:
            rows = self.con.execute(
                "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (q.tobytes(), k)).fetchall()
            return [(r[0], 1 - r[1] ** 2 / 2) for r in rows]  # L2 on unit vectors -> cosine
        rows = self.con.execute("SELECT id, v FROM chunks_blob").fetchall()
        if not rows:
            return []
        ids = np.array([r[0] for r in rows])
        m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        sims = m @ q
        top = np.argsort(-sims)[:k]
        return [(int(ids[i]), float(sims[i])) for i in top]


def chunk_page(lines: list[str]) -> list[tuple[int, int, str]]:
    out = []
    if not lines:
        return out
    start = 0
    while True:
        end = min(len(lines), start + CHUNK_LINES)
        text = "\n".join(lines[start:end]).strip()
        if len(text) >= MIN_CHUNK_CHARS:
            out.append((start + 1, end, text))
        if end >= len(lines):
            break
        start += CHUNK_STRIDE
    return out


def connect_index(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def index_book(cfg: Config, con: sqlite3.Connection, sha: str, title: str, emb: Embedder,
               store: VectorStore, force: bool = False) -> dict:
    obj = cfg.objects / sha
    mpath = obj / "extract.json"
    if not mpath.is_file():
        return {"sha256": sha, "error": "not extracted"}
    extracted_at = json.loads(mpath.read_text(encoding="utf-8"))["extracted_at"]
    st = con.execute("SELECT extracted_at, embed_spec FROM index_state WHERE sha256=?", (sha,)).fetchone()
    if st and st[0] == extracted_at and st[1] == emb.spec and not force:
        log.info("skip (indexed): %s", title)
        return {"sha256": sha, "skipped": True}

    t = Timer()
    old_ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE sha256=?", (sha,))]
    store.delete(old_ids)
    con.execute("DELETE FROM chunks WHERE sha256=?", (sha,))
    con.execute("DELETE FROM pages_fts WHERE sha256=?", (sha,))

    pages = read_pages(obj)
    con.executemany("INSERT INTO pages_fts(text, sha256, pdf_page) VALUES(?,?,?)",
                    [("\n".join(lines), sha, p) for p, lines in pages.items() if lines])
    rows = [(sha, p, a, b, text) for p, lines in pages.items() for a, b, text in chunk_page(lines)]
    ids = []
    for r in rows:
        ids.append(con.execute("INSERT INTO chunks(sha256, pdf_page, line_start, line_end, text) VALUES(?,?,?,?,?)",
                               r).lastrowid)
    B = 512
    for i in range(0, len(rows), B):
        store.add(ids[i:i + B], emb.docs([r[4] for r in rows[i:i + B]]))
    con.execute("INSERT OR REPLACE INTO index_state VALUES(?,?,?,?,?)", (sha, extracted_at, emb.spec, len(rows), now()))
    con.execute("UPDATE books SET status='indexed', updated_at=? WHERE sha256=? AND status IN ('extracted','extracted-partial')", (now(), sha))
    con.commit()
    log.info("index: %s — %d pages, %d chunks in %.1fs", title, len(pages), len(rows), t.s)
    return {"sha256": sha, "pages": len(pages), "chunks": len(rows), "seconds": round(t.s, 1)}
