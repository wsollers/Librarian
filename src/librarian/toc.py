"""Tables of contents read by Codex, for books whose contents pages the parser cannot read
(two-column layouts, 'Chapter XIX' headings with sections numbered from 1, missing dot leaders ...).

  librarian toc --weak                 # every book whose sections look wrong (Physics and Calculus excluded)
  librarian toc --select Dieudonn      # these books, weak or not
  librarian toc --weak --dry-run       # list them only

The contents pages are found in the front matter and their text (no images) goes to Codex, which
returns the entries as JSON; they are saved as objects/<sha>/toc.json, which the sections step
prefers from then on (so pagemap reruns keep them), and the book's sections and profile are rebuilt.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from . import sections
from .pagemap import read_pages

EXCLUDE = ("Physics/", "Calculus/")
MIN_PAGES = 60
MAX_CHARS = 45_000
_CONTENTS = re.compile(r"^\W*(table of )?contents\b|^\W*(inhalt|inhaltsverzeichnis|sommaire|table des mati[eè]res)\b", re.I)
_ENDS_NUM = re.compile(r"(\d+|[ivxlc]+)\s*$", re.I)

PROMPT = """contents.md in the current directory holds the text of the table-of-contents pages of a
mathematics book (OCR or PDF text: columns may be split, page numbers may sit on separate lines).
Reconstruct the table of contents. Reply with ONE JSON object and nothing else:

{"entries": [{"level": 1, "number": "1", "title": "Metric spaces", "page": "17"}, ...]}

* level: 0 part, 1 chapter (and front/back matter such as Preface, Bibliography, Index), 2 section,
  3 subsection. Leave out anything deeper.
* number: as the book numbers it. Chapter numbers as printed (roman stays roman). When sections are
  numbered from 1 inside each chapter and the book cites them with the chapter (for example section 3
  of chapter XIX is cited 19.3), write the qualified form "19.3". null for unnumbered entries.
* title: as printed, without the number, dot leaders or page number.
* page: the printed page number exactly as given in the contents (roman for front matter); null when
  the contents gives none (for example sections run together in one paragraph under a chapter:
  list each of them anyway).
* Ignore anything on these pages that is not the contents (notation lists, prefaces).
* Keep the book's order. Include every chapter and section listed; do not invent entries.
"""


def is_weak(con: sqlite3.Connection, sha: str) -> bool:
    n, v = con.execute("SELECT count(*), coalesce(sum(verified), 0) FROM sections WHERE sha256=?", (sha,)).fetchone()
    return n < 8 or v < 0.3 * n


def candidates(con: sqlite3.Connection, select: Optional[str], weak_only: bool) -> list[tuple]:
    rows = con.execute("SELECT sha256, path, title, pdf_pages FROM books WHERE status='indexed' ORDER BY path").fetchall()
    out = []
    for sha, path, title, pages in rows:
        if select and select.lower() not in path.lower() and not sha.startswith(select.lower()):
            continue
        if not select and (path.startswith(EXCLUDE) or (pages or 0) < MIN_PAGES):
            continue
        if weak_only and not is_weak(con, sha):
            continue
        out.append((sha, path, title))
    return out


def contents_pages(pages: dict[int, list[str]], body_start: Optional[int]) -> list[int]:
    """The run of contents pages: from a 'Contents' heading (or the most entry-like page) in the front
    matter, continuing while pages keep looking like contents (at most 16 pages)."""
    limit = min(max(pages) if pages else 0, (body_start - 1) if body_start and body_start > 5 else 60, 60)
    def score(p):
        ls = [l for l in pages.get(p, []) if l.strip()]
        return sum(1 for l in ls if _ENDS_NUM.search(l)) / max(len(ls), 1) if len(ls) >= 5 else 0.0
    heads = [p for p in range(1, limit + 1) if any(_CONTENTS.match(l.strip("#* ")) for l in pages.get(p, [])[:8])]
    start = heads[0] if heads else max(range(1, limit + 1), key=score, default=None)
    if not start:
        return []
    run, gap = [start], 0
    for p in range(start + 1, min(start + 16, max(pages) + 1)):
        if score(p) >= 0.25 or any(_CONTENTS.match(l.strip("#* ")) for l in pages.get(p, [])[:5]):
            run.append(p)
            gap = 0
        else:
            gap += 1
            if gap > 1:
                break
    return run


def make(cfg, con: sqlite3.Connection, sha: str) -> dict:
    from .ingest import codex_json
    obj = cfg.objects / sha
    pages = read_pages(obj)
    body = sections.parts_of(con, sha)
    pp = contents_pages(pages, body[0] if body else None)
    if not pp:
        return {"sha256": sha, "error": "no contents pages found"}
    text = "\n\n".join(f"=== PDF page {p} ===\n" + "\n".join(pages[p]) for p in pp)[:MAX_CHARS]
    res = codex_json(PROMPT, {"contents.md": text}, [], timeout=900)
    if not res or not res.get("entries"):
        return {"sha256": sha, "error": "Codex returned no entries", "pages": pp}
    res["contents_pages"] = pp
    (obj / "toc.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"sha256": sha, "entries": len(res["entries"]), "pages": pp}


def run(cfg, con: sqlite3.Connection, select: Optional[str] = None, weak_only: bool = True, force: bool = False,
        dry_run: bool = False, workers: int = 2) -> dict:
    from . import profiles
    books = candidates(con, select, weak_only)
    if not force:
        books = [b for b in books if not (cfg.objects / b[0] / "toc.json").is_file()]
    say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    say(f"toc: {len(books)} book(s)")
    if dry_run:
        for sha, path, _ in books:
            n, v = con.execute("SELECT count(*), coalesce(sum(verified),0) FROM sections WHERE sha256=?", (sha,)).fetchone()
            say(f"  {n:>3} sections ({v} verified)  {path}")
        return {"books": len(books), "dry_run": True}
    done, failed, before_after = 0, [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_make_safe, cfg, sha): (sha, path, title) for sha, path, title in books}
        for fut in as_completed(futs):
            sha, path, title = futs[fut]
            r = fut.result()
            if r.get("error"):
                failed.append(f"{path}: {r['error']}")
                say(f"  FAILED {path}: {r['error']}")
                continue
            n0 = con.execute("SELECT count(*) FROM sections WHERE sha256=?", (sha,)).fetchone()[0]
            s = sections.build(cfg, con, sha, title, path)
            try:
                profiles.refresh(con, sha)
            except Exception:  # noqa: BLE001
                pass
            con.commit()
            done += 1
            before_after.append((path, n0, s.get("sections"), s.get("verified")))
            say(f"  {path}: {n0} -> {s.get('sections')} sections ({s.get('verified')} verified, {s.get('source')})")
    return {"books": len(books), "done": done, "failed": failed,
            "changes": [{"book": p, "before": a, "after": b, "verified": v} for p, a, b, v in before_after]}


def _make_safe(cfg, sha: str) -> dict:
    """make() in a worker thread: its own read connection (sqlite connections stay in their thread)."""
    from .db import connect
    con = connect(cfg.db_path)
    try:
        return make(cfg, con, sha)
    except Exception as e:  # noqa: BLE001
        return {"sha256": sha, "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()
