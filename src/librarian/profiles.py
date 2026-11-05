"""Book profiles: what a book is about, from its table of contents and its preface or introduction.

One row per indexed book in `book_profiles`, searchable through `profiles_fts`, so "which books
cover Fourier analysis?" is answered from what the books say about themselves rather than from
the folder they were filed in. Profiles also feed the subject classification (reports/dossiers.jsonl).

Built by `librarian profiles` (all or a selection) and refreshed for each book the index stage
indexes. They read the parsed sections and the indexed page text, so they need pagemap and index.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .db import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_profiles (
    sha256     TEXT PRIMARY KEY,
    toc        TEXT NOT NULL,     -- JSON list of chapter lines ("5 Compactness"), sections when chapters are few
    intro_from TEXT,              -- the TOC entry the intro text came from, or 'opening pages'
    intro      TEXT NOT NULL,     -- preface / introduction text (cleaned, capped at INTRO_CHARS)
    built_at   TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS profiles_fts USING fts5(
    title, author, folder, toc, intro, sha256 UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

INTRO_CHARS = 6000        # stored per book
INTRO_PAGES = 3           # pages read from the start of a preface / introduction
MIN_INTRO = 300           # shorter than this: use the opening pages instead
MAX_TOC = 60
_INTRO = re.compile(r"\b(preface|introduction|foreword|prologue|to the reader|overview|avant-propos)\b", re.I)


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


RowMap = dict[tuple[str, int], int]


def row_map(con: sqlite3.Connection, sha: Optional[str] = None) -> RowMap:
    """(book, pdf page) -> pages_fts row. The sha256/pdf_page columns are not indexed, so this is one
    scan (~0.6 s for the whole library); build it once and reuse it."""
    sql, args = "SELECT id, c1, c2 FROM pages_fts_content", []
    if sha is not None:
        sql, args = sql + " WHERE c1 = ?", [sha]
    return {(s, int(p)): r for r, s, p in con.execute(sql, args)}


def _page_text(con: sqlite3.Connection, rows: RowMap, sha: str, pages: Iterable[int]) -> list[str]:
    out = []
    for p in pages:
        r = rows.get((sha, p))
        got = con.execute("SELECT c0 FROM pages_fts_content WHERE id = ?", (r,)).fetchone() if r else None
        out.append(got[0] if got else "")
    return out


def build_profile(con: sqlite3.Connection, sha: str, rows: Optional[RowMap] = None) -> Optional[dict]:
    """The profile of one indexed book (not stored), or None if the book has no indexed text.
    `rows`: a row_map covering this book (default: built for this book alone)."""
    rows = row_map(con, sha) if rows is None else rows
    book = con.execute("SELECT title, author, path, pdf_pages FROM books WHERE sha256 = ?", (sha,)).fetchone()
    if book is None:
        return None
    title, author, path, n_pages = book
    try:
        secs = con.execute("SELECT level, number, title, pdf_start, pdf_end FROM sections WHERE sha256 = ? "
                           "ORDER BY seq", (sha,)).fetchall()
    except sqlite3.OperationalError:    # no sections table yet
        secs = []
    line = lambda n, t: f"{(n or '').strip()} {(t or '').strip()}".strip()
    toc = [line(n, t) for lv, n, t, a, b in secs if lv is not None and lv <= 1]
    if len(toc) < 6:                    # few chapters: list the sections too
        toc = [line(n, t) for lv, n, t, a, b in secs]
    toc = [t for t in toc if t][:MAX_TOC]

    intro, intro_from = "", None
    for lv, n, t, a, b in secs:
        if a and _INTRO.search(t or ""):
            a = int(a)
            end = min(int(b or a), a + INTRO_PAGES - 1)
            text = _clean("\n".join(_page_text(con, rows, sha, range(a, end + 1))))
            if len(text) >= MIN_INTRO:
                intro, intro_from = text, (t or "").strip()
                break
    if len(intro) < MIN_INTRO:          # no usable preface: the first pages with real text
        texts = _page_text(con, rows, sha, range(1, min(n_pages or 25, 25) + 1))
        intro = _clean("\n".join(t for t in texts if len(t) > 400))
        intro_from = "opening pages"
    if not intro and not toc:
        return None
    return {"sha256": sha, "title": title, "author": author,
            "folder": str(Path(path).parent).replace("\\", "/"), "pdf_pages": n_pages,
            "toc": toc, "intro_from": intro_from, "intro": intro[:INTRO_CHARS]}


def store(con: sqlite3.Connection, p: dict) -> None:
    con.execute("INSERT INTO book_profiles(sha256, toc, intro_from, intro, built_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(sha256) DO UPDATE SET toc=excluded.toc, intro_from=excluded.intro_from, "
                "intro=excluded.intro, built_at=excluded.built_at",
                (p["sha256"], json.dumps(p["toc"], ensure_ascii=False), p["intro_from"], p["intro"], now()))
    con.execute("DELETE FROM profiles_fts WHERE sha256 = ?", (p["sha256"],))
    con.execute("INSERT INTO profiles_fts(title, author, folder, toc, intro, sha256) VALUES(?,?,?,?,?,?)",
                (p["title"] or "", p["author"] or "", p["folder"], "\n".join(p["toc"]), p["intro"], p["sha256"]))


def refresh(con: sqlite3.Connection, sha: str, rows: Optional[RowMap] = None) -> bool:
    """Rebuild and store one book's profile; False if it has nothing to profile yet."""
    ensure(con)
    p = build_profile(con, sha, rows)
    if p is None:
        con.execute("DELETE FROM book_profiles WHERE sha256 = ?", (sha,))
        con.execute("DELETE FROM profiles_fts WHERE sha256 = ?", (sha,))
        return False
    store(con, p)
    return True


def build_all(con: sqlite3.Connection, shas: Optional[list[str]] = None) -> dict:
    """Profiles for the given books (default: every indexed book)."""
    ensure(con)
    if shas is None:
        shas = [r[0] for r in con.execute("SELECT sha256 FROM index_state ORDER BY sha256")]
    rows = row_map(con)
    made = sum(1 for sha in shas if refresh(con, sha, rows))
    con.commit()
    return {"profiles": made, "without_text": len(shas) - made}


def load(con: sqlite3.Connection, sha: str) -> Optional[dict]:
    row = con.execute("SELECT p.toc, p.intro_from, p.intro, p.built_at, b.title, b.author, b.path, b.pdf_pages "
                      "FROM book_profiles p JOIN books b USING (sha256) WHERE p.sha256 = ?", (sha,)).fetchone()
    if row is None:
        return None
    toc, intro_from, intro, built_at, title, author, path, n_pages = row
    return {"sha256": sha, "title": title, "author": author, "folder": str(Path(path).parent).replace("\\", "/"),
            "pdf_pages": n_pages, "toc": json.loads(toc), "intro_from": intro_from, "intro": intro,
            "built_at": built_at}


def find(con: sqlite3.Connection, query: str, k: int = 20, shas: Optional[set[str]] = None) -> list[dict]:
    """Books whose title, chapters or preface match `query` (FTS5 syntax; plain words are ANDed)."""
    from .search import fts_query
    sql = ("SELECT sha256, title, author, folder, snippet(profiles_fts, -1, '[', ']', ' … ', 16), "
           "bm25(profiles_fts, 10.0, 2.0, 1.0, 4.0, 1.0) AS score FROM profiles_fts WHERE profiles_fts MATCH ?")
    args: list = [fts_query(query, "fts")]
    if shas is not None:
        if not shas:
            return []
        sql += f" AND sha256 IN ({','.join('?' * len(shas))})"
        args += sorted(shas)
    rows = con.execute(sql + " ORDER BY score LIMIT ?", args + [max(1, min(k, 200))]).fetchall()
    return [{"sha256": r[0], "title": r[1], "author": r[2], "folder": r[3], "match": r[4]} for r in rows]


def dossiers(con: sqlite3.Connection, out: Path, intro_chars: int = 1800) -> int:
    """Write one JSON line per profile (intro shortened) for classifying the books into subjects."""
    rows = con.execute("SELECT sha256 FROM book_profiles ORDER BY sha256").fetchall()
    lines = []
    for (sha,) in rows:
        p = load(con, sha)
        if p:
            lines.append(json.dumps({"sha": sha[:16], "title": p["title"], "author": p["author"],
                                     "folder": p["folder"], "pdf_pages": p["pdf_pages"], "toc": p["toc"],
                                     "intro_from": p["intro_from"], "intro": p["intro"][:intro_chars]},
                                    ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
