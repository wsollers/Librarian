"""Hybrid search: FTS5 phrase/keyword over pages + vector KNN over chunks, merged by RRF.

Every hit carries book, printed page, PDF page and a line span of the page file, and the
page file path, so a reader (or the skill) can open the page before asserting anything.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Optional

from .config import Config
from .embed import Embedder
from .index import VectorStore

RRF_K = 60
DEF_BOOST = 1.5   # pages where the matched lines read like a definition
_DEF_CUE = re.compile(r"\b(defin\w*|is called|are called|we say|we call|known as|is said to be|denote[sd]?)\b", re.I)
_TOKEN = re.compile(r"\w+", re.UNICODE)
STOP = set("a an the of to in on for and or is are be by with as at that this what which where when "
           "how type kind set value change constant".split())


@dataclass
class Hit:
    score: float
    title: str
    author: Optional[str]
    printed_page: Optional[str]
    pdf_page: int
    line_start: int
    line_end: int
    via: str
    snippet: str
    page_file: str
    sha256: str


def fts_query(q: str, mode: str) -> str:
    if mode == "phrase" or (q.startswith('"') and q.endswith('"')):
        return '"' + q.strip('"').replace('"', "") + '"'
    toks = [t for t in _TOKEN.findall(q.lower()) if t not in STOP] or _TOKEN.findall(q.lower())
    return " OR ".join(f'"{t}"' for t in toks)


def line_span(lines: list[str], q: str) -> tuple[int, int]:
    """Lines matching the phrase (across line breaks) or, failing that, the densest term window."""
    phrase = " ".join(_TOKEN.findall(q.lower()))
    starts, buf = [], ""
    for i, ln in enumerate(lines):
        starts.append(len(buf))
        buf += " ".join(_TOKEN.findall(ln.lower())) + " "
    pos = buf.find(phrase) if phrase else -1
    if pos >= 0:
        a = max(i for i, s in enumerate(starts) if s <= pos)
        b = max(i for i, s in enumerate(starts) if s <= pos + len(phrase) - 1)
        return a + 1, b + 1
    terms = {t for t in _TOKEN.findall(q.lower()) if t not in STOP}
    scores = [len(terms & set(_TOKEN.findall(ln.lower()))) for ln in lines]
    if not any(scores):
        return 1, min(len(lines), 3)
    best = max(range(len(lines)), key=lambda i: sum(scores[i:i + 4]))
    hits = [i for i in range(best, min(len(lines), best + 4)) if scores[i]]
    return hits[0] + 1, hits[-1] + 1


def search(cfg: Config, con: sqlite3.Connection, q: str, emb: Optional[Embedder], mode: str = "hybrid",
           k: int = 10, book: Optional[str] = None) -> list[Hit]:
    ranked: dict[tuple[str, int], dict] = {}
    bfilter, bargs = ("", [])
    if book:
        shas = [r[0] for r in con.execute(
            "SELECT sha256 FROM books WHERE title LIKE ? OR author LIKE ? OR path LIKE ? OR sha256 LIKE ?",
            (f"%{book}%",) * 3 + (f"{book}%",))]
        if not shas:
            return []
        bfilter, bargs = f" AND sha256 IN ({','.join('?' * len(shas))})", shas

    if mode in ("hybrid", "phrase", "fts"):
        rows = con.execute(
            f"SELECT sha256, pdf_page, text, bm25(pages_fts) FROM pages_fts WHERE pages_fts MATCH ?{bfilter} "
            "ORDER BY bm25(pages_fts) LIMIT 50", [fts_query(q, mode)] + bargs).fetchall()
        for rank, (sha, p, text, _) in enumerate(rows):
            a, b = line_span(text.split("\n"), q)
            ranked.setdefault((sha, int(p)), {"score": 0.0, "via": set(), "span": (a, b)})
            ranked[(sha, int(p))]["score"] += 1 / (RRF_K + rank)
            ranked[(sha, int(p))]["via"].add("fts")

    if mode in ("hybrid", "semantic") and emb is not None:
        store = VectorStore(con, emb.dim, emb.spec, create=False)
        nn = store.knn(emb.query(q), 200 if book else 50)
        rank = 0
        for cid, _sim in nn:
            sha, p, a, b = con.execute("SELECT sha256, pdf_page, line_start, line_end FROM chunks WHERE id=?",
                                       (cid,)).fetchone()
            if bargs and sha not in bargs:
                continue
            e = ranked.setdefault((sha, p), {"score": 0.0, "via": set(), "span": (a, b)})
            if "vec" not in e["via"]:
                e["score"] += 1 / (RRF_K + rank)
                e["via"].add("vec")
                if "fts" not in e["via"]:
                    e["span"] = (a, b)
                rank += 1

    # Definitional pages first: a reader asking "what is X" wants where X is defined.
    for (sha, p), e in ranked.items():
        f = cfg.objects / sha / "pages" / f"{p:04d}.md"
        if f.is_file():
            lines = f.read_text(encoding="utf-8").split("\n")
            a, b = e["span"]
            window = " ".join(lines[max(0, a - 3):b + 1])
            if _DEF_CUE.search(window):
                e["score"] *= DEF_BOOST
                e["via"].add("def")
    hits, seen = [], set()
    for (sha, p), e in sorted(ranked.items(), key=lambda kv: -kv[1]["score"]):
        if len(hits) >= k:
            break
        title, author = con.execute("SELECT title, author FROM books WHERE sha256=?", (sha,)).fetchone()
        printed = con.execute("SELECT printed_page FROM pages WHERE sha256=? AND pdf_page=?", (sha, p)).fetchone()
        page_file = cfg.objects / sha / "pages" / f"{p:04d}.md"
        lines = page_file.read_text(encoding="utf-8").split("\n") if page_file.is_file() else []
        a, b = e["span"]
        snippet = " ".join(ln.strip() for ln in lines[a - 1:b])[:300]
        key = (p, "\n".join(lines))  # same page text in two files (re-downloaded copies): show once
        if key in seen:
            continue
        seen.add(key)
        hits.append(Hit(round(e["score"], 5), title, author, printed[0] if printed else None, p, a, b,
                        "+".join(sorted(e["via"])), snippet, str(page_file), sha))
    return hits


def format_hit(h: Hit) -> str:
    pp = f"p. {h.printed_page}" if h.printed_page else "p. ?"
    return (f"{h.title} — {h.author or '?'}: {pp} (PDF {h.pdf_page}), lines {h.line_start}–{h.line_end} [{h.via}]\n"
            f"    {h.snippet}\n    {h.page_file}")


def as_dict(h: Hit) -> dict:
    return asdict(h)
