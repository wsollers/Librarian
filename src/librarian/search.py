"""Search: candidates from FTS5 (phrase/keywords) and vector KNN, then a cross-encoder rerank.

* Quoted or short queries are treated as text: FTS leads.
* Descriptive queries ("the kind of continuity where ...") are treated as meaning: vectors
  generate candidates and FTS only contributes rarer terms; the cross-encoder decides.
Every hit carries book, printed page, PDF page and a line span of the page file, and the
page file path, so a reader (or the skill) can open the page before asserting anything.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Optional

from .config import Config
from .embed import Embedder
from .index import VectorStore

RRF_K = 60
DEF_BOOST = 0.05      # added when the cited lines themselves define a term (reranked scores)
DEF_BOOST_RRF = 0.15  # same, without a reranker (rank-fusion scores are flatter)
CONTEXT = 4       # lines of context either side of a chunk given to the reranker
WINDOW = 4        # citation granularity: best 4-line window inside the passage
REFINE_N = 25     # how many top candidates get line-level refinement
# Phrases that introduce a definition, not every use of "defined"/"definition of".
_DEF_CUE = re.compile(
    r"(\bdefinition\s+\d|\bdefinition\b\s*[:.(]|\bis called\b|\bare called\b|\bwe say\b|\bwe call\b|"
    r"\bknown as\b|\bis said to be\b|\bare said to be\b|\bdefine\b[^.;]{0,60}?\bto be\b|\bwe define\b)", re.I)
_TOKEN = re.compile(r"\w+", re.UNICODE)
STOP = set("""a an the of to in on for and or is are be by with as at that this what which where when how
who why do does it its from into than then there their these those such some any all can may
type kind sort set value change changes constant called mean means name named""".split())
VEC_K = 200
FTS_K = 50
RERANK_N = 80


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


@dataclass
class Cand:
    sha: str
    page: int
    span: tuple[int, int]
    rrf: float = 0.0
    via: set = field(default_factory=set)
    passage: str = ""
    rerank: Optional[float] = None
    region: tuple[int, int] = (1, 1)


def is_descriptive(q: str) -> bool:
    """A question or a description (meaning), as opposed to a phrase or a few keywords (text)."""
    if q.startswith('"') and q.endswith('"'):
        return False
    words = _TOKEN.findall(q.lower())
    return len(words) >= 6 or (bool(words) and words[0] in {"what", "which", "how", "why", "where", "when", "who"})


def fts_query(q: str, mode: str) -> str:
    if mode == "phrase" or (q.startswith('"') and q.endswith('"')):
        return '"' + q.strip('"').replace('"', "") + '"'
    toks = [t for t in _TOKEN.findall(q.lower()) if t not in STOP] or _TOKEN.findall(q.lower())
    return " OR ".join(f'"{t}"' for t in toks)


def line_span(lines: list[str], q: str) -> tuple[int, int]:
    """Lines matching the phrase (across line breaks) or, failing that, the densest term window."""
    phrase = " ".join(_TOKEN.findall(q.lower()))
    starts, buf = [], ""
    for ln in lines:
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


def _page_lines(cfg: Config, sha: str, p: int) -> list[str]:
    f = cfg.objects / sha / "pages" / f"{p:04d}.md"
    return f.read_text(encoding="utf-8").split("\n") if f.is_file() else []


def search(cfg: Config, con: sqlite3.Connection, q: str, emb: Optional[Embedder], mode: str = "hybrid",
           k: int = 10, book: Optional[str] = None, reranker=None, explain: int = 0) -> list[Hit]:
    descriptive = mode in ("hybrid", "semantic") and is_descriptive(q)
    cands: dict[tuple[str, int], Cand] = {}
    bargs: list[str] = []
    bfilter = ""
    if book:
        bargs = [r[0] for r in con.execute(
            "SELECT sha256 FROM books WHERE title LIKE ? OR author LIKE ? OR path LIKE ? OR sha256 LIKE ?",
            (f"%{book}%",) * 3 + (f"{book}%",))]
        if not bargs:
            return []
        bfilter = f" AND sha256 IN ({','.join('?' * len(bargs))})"

    if mode in ("hybrid", "phrase", "fts"):
        fts_w = 0.5 if descriptive else 1.0
        rows = con.execute(
            f"SELECT sha256, pdf_page, text FROM pages_fts WHERE pages_fts MATCH ?{bfilter} "
            f"ORDER BY bm25(pages_fts) LIMIT {FTS_K}", [fts_query(q, mode)] + bargs).fetchall()
        for rank, (sha, p, text) in enumerate(rows):
            p = int(p)
            c = cands.setdefault((sha, p), Cand(sha, p, line_span(text.split("\n"), q)))
            c.rrf += fts_w / (RRF_K + rank)
            c.via.add("fts")

    if mode in ("hybrid", "semantic") and emb is not None:
        store = VectorStore(con, emb.dim, emb.spec, create=False)
        rank = 0
        for cid, _sim in store.knn(emb.query(q), VEC_K * (3 if book else 1)):
            sha, p, a, b, text = con.execute(
                "SELECT sha256, pdf_page, line_start, line_end, text FROM chunks WHERE id=?", (cid,)).fetchone()
            if bargs and sha not in bargs:
                continue
            c = cands.setdefault((sha, p), Cand(sha, p, (a, b)))
            if "vec" in c.via:
                continue
            c.rrf += 1 / (RRF_K + rank)
            c.via.add("vec")
            if "fts" not in c.via or descriptive:
                c.span, c.passage = (a, b), text
            rank += 1

    ordered = sorted(cands.values(), key=lambda c: -c.rrf)
    if explain:
        import sys
        print(f"# {len(ordered)} candidates, descriptive={descriptive}; top {explain} before rerank:", file=sys.stderr)
        for i, c in enumerate(ordered[:explain], 1):
            t = con.execute("SELECT title FROM books WHERE sha256=?", (c.sha,)).fetchone()[0]
            print(f"#  {i:3d} {c.rrf:.4f} {'+'.join(sorted(c.via)):8s} {t[:40]:40s} PDF {c.page}", file=sys.stderr)
    pool = ordered[:RERANK_N] if reranker else ordered
    lines_cache: dict[tuple[str, int], list[str]] = {}
    for c in pool:
        lines = lines_cache.setdefault((c.sha, c.page), _page_lines(cfg, c.sha, c.page))
        a, b = c.span
        c.region = (max(1, a - CONTEXT), min(len(lines), b + CONTEXT)) if lines else (a, b)
        c.passage = "\n".join(lines[c.region[0] - 1:c.region[1]]) or c.passage

    qq = q.strip('"')
    if reranker:
        for c, s in zip(pool, reranker.scores(qq, [c.passage for c in pool])):
            c.rerank = _sigmoid(s)
            c.via.add("rr")
        pool.sort(key=lambda c: -c.rerank)
        refine(pool[:REFINE_N], lines_cache, qq, reranker)
    else:
        top = pool[0].rrf if pool else 1
        for c in pool:
            c.rerank = c.rrf / top
    for c in pool:
        lines = lines_cache.get((c.sha, c.page), [])
        a, b = c.span
        if _DEF_CUE.search(" ".join(lines[max(0, a - 2):b])):  # the cited lines (and the one before)
            c.via.add("def")
            c.rerank += DEF_BOOST if reranker else DEF_BOOST_RRF
    pool.sort(key=lambda c: -c.rerank)

    hits, seen = [], set()
    for c in pool:
        if len(hits) >= k:
            break
        lines = lines_cache.get((c.sha, c.page)) or _page_lines(cfg, c.sha, c.page)
        key = (c.page, "\n".join(lines))  # same page text in two files (duplicate copies): show once
        if key in seen:
            continue
        seen.add(key)
        title, author = con.execute("SELECT title, author FROM books WHERE sha256=?", (c.sha,)).fetchone()
        printed = con.execute("SELECT printed_page FROM pages WHERE sha256=? AND pdf_page=?", (c.sha, c.page)).fetchone()
        a, b = c.span
        snippet = " ".join(ln.strip() for ln in lines[a - 1:b])[:300]
        hits.append(Hit(round(c.rerank, 4), title, author, printed[0] if printed else None, c.page, a, b,
                        "+".join(sorted(c.via)), snippet,
                        str(cfg.objects / c.sha / "pages" / f"{c.page:04d}.md"), c.sha))
    return hits


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def windows(region: tuple[int, int], size: int = WINDOW) -> list[tuple[int, int]]:
    a, b = region
    if b - a + 1 <= size:
        return [(a, b)]
    return [(i, i + size - 1) for i in range(a, b - size + 2)]


def refine(cands: list["Cand"], lines_cache: dict, q: str, reranker) -> None:
    """Pin each candidate to its best WINDOW-line span and blend that score in."""
    jobs = []
    for c in cands:
        lines = lines_cache.get((c.sha, c.page), [])
        for w in windows(c.region):
            text = "\n".join(lines[w[0] - 1:w[1]]).strip()
            if text:
                jobs.append((c, w, text))
    scores = reranker.scores(q, [t for _, _, t in jobs])
    best: dict[int, tuple[float, tuple[int, int]]] = {}
    for (c, w, _), s in zip(jobs, scores):
        if id(c) not in best or s > best[id(c)][0]:
            best[id(c)] = (s, w)
    for c in cands:
        if id(c) in best:
            s, w = best[id(c)]
            c.span = w
            c.rerank = 0.5 * c.rerank + 0.5 * _sigmoid(s)


def format_hit(h: Hit) -> str:
    pp = f"p. {h.printed_page}" if h.printed_page else "p. ?"
    return (f"{h.title} — {h.author or '?'}: {pp} (PDF {h.pdf_page}), lines {h.line_start}–{h.line_end} "
            f"[{h.via} {h.score}]\n    {h.snippet}\n    {h.page_file}")


def as_dict(h: Hit) -> dict:
    return asdict(h)
