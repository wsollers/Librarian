"""Chapters and sections per book.

Primary source: the table of contents. Born-digital pages are re-read with `pdftotext -layout`
(plain extraction splits titles and page numbers into separate columns); OCR'd pages are used
as they are. Each entry's printed page goes through the page map to a PDF page, and the entry
is checked against that page's first lines (searching +/-2 pages to correct drift).
Fallback when no contents page parses: numbered headings at the top of pages ("3.4 Uniform ...").
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .log import log
from .pagemap import read_pages
from .text import _LIGATURES

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    sha256       TEXT NOT NULL,
    seq          INTEGER NOT NULL,   -- order in the book
    level        INTEGER NOT NULL,   -- 0 part, 1 chapter / front-back matter, 2 section, 3 subsection
    number       TEXT,               -- '9.9', '1', 'A'; NULL for Preface, Index, ...
    title        TEXT NOT NULL,
    printed_page TEXT,
    pdf_start    INTEGER NOT NULL,
    pdf_end      INTEGER NOT NULL,
    source       TEXT NOT NULL,      -- toc | heads
    verified     INTEGER NOT NULL,   -- 1 if the title was found on pdf_start
    PRIMARY KEY (sha256, seq)
);
CREATE INDEX IF NOT EXISTS sections_page ON sections(sha256, pdf_start, pdf_end);
"""

_WORDNUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_PAGE_TAIL = re.compile(
    r"^(?P<title>.*?\S)\s*(?:(?:\.\s?){2,}|\s{2,}|\s(?=[0-9ivxlc]+\s*$))\s*(?P<page>\d{1,4}|[ivxlc]{1,7})\s*$", re.I)
_NUM = re.compile(
    r"^(?:(?P<kw>chapter|part|appendix|lecture|lesson)\s+(?P<kwn>[0-9]+|[A-Z]|[ivxlc]+|[a-z]+)\b[.:]?\s*"
    r"|§?\s*(?P<dot>\d{1,2}(?:\.\d{1,2}){0,3})\.?(?=\s)\s*"
    r"|(?P<rom>[IVX]{1,5})\.\s+"
    r"|(?P<let>[A-H])\.(?=\s)\s*)", re.I)
_HEAD = re.compile(r"^\s*(?:§\s*)?(?P<num>\d{1,2}(?:\.\d{1,2}){1,2})\.?\s+(?P<title>[A-Z][^|]{2,80}?)\s*$")
_WORD = re.compile(r"[a-z]{3,}")
_CONTENTS = re.compile(r"^\s*(table of\s+)?contents\s*$", re.I)


@dataclass
class Entry:
    level: int
    number: Optional[str]
    title: str
    printed: str
    pdf: Optional[int] = None
    verified: bool = False


def parse_entry(line: str) -> Optional[Entry]:
    line = line.translate(_LIGATURES).rstrip()
    raw = line.replace("|", "   ").strip()
    m = _PAGE_TAIL.match(raw)
    if not m:
        return None
    title, page = m.group("title").strip(" .|*#"), m.group("page")
    if not re.search(r"[A-Za-z]{3}", title) or len(title) > 140:
        return None
    level, number = 1, None
    n = _NUM.match(title)
    if n:
        rest = title[n.end():].strip(" .:-—")
        if n.group("kw"):
            kw, num = n.group("kw").lower(), n.group("kwn")
            number = str(_WORDNUM.get(num.lower(), num)).upper() if not num.isdigit() else num
            level = 0 if kw == "part" else 1
        elif n.group("dot"):
            number = n.group("dot")
            level = min(3, number.count(".") + 1)
        elif n.group("rom"):
            number, level = n.group("rom").upper(), 0
        elif n.group("let"):
            number, level = n.group("let").upper(), 1
        title = rest or title
    if not re.search(r"[A-Za-z]{3}", title):
        return None
    return Entry(level, number, title.strip(), page.lower())


def layout_text(pdf: Path, page: int) -> str:
    r = subprocess.run(["pdftotext", "-q", "-layout", "-enc", "UTF-8", "-f", str(page), "-l", str(page), str(pdf), "-"],
                       capture_output=True, timeout=60)
    return r.stdout.decode("utf-8", "replace")


def toc_entries(pdf: Optional[Path], pages: dict[int, list[str]], methods: dict[int, str]) -> list[Entry]:
    """Parse the contents pages: those in the first fifth of the book that parse as >= 5 entries,
    starting at a 'Contents' heading or the first such page, continuing while pages keep parsing."""
    n = max(pages) if pages else 0
    entries: list[Entry] = []
    started = False
    for p in range(1, max(1, min(n, 60, n // 5 + 10)) + 1):
        use_layout = pdf is not None and methods.get(p) == "textlayer"
        lines = layout_text(pdf, p).splitlines() if use_layout else pages.get(p, [])
        got = [e for e in (parse_entry(ln) for ln in lines) if e]
        heading = any(_CONTENTS.match(ln) for ln in lines[:6])
        if (heading and len(got) >= 3) or len(got) >= 6:
            entries += got
            started = True
        elif started and len(got) < 3:
            break
    return entries


def printed_to_pdf(printed: str, mapping: dict[int, Optional[str]]) -> Optional[int]:
    exact = [p for p, v in mapping.items() if v == printed]
    if exact:
        return min(exact)
    if printed.isdigit():  # unmapped region: use the nearest page that has an arabic number
        target = int(printed)
        known = [(p, int(v)) for p, v in mapping.items() if v and v.isdigit()]
        if known:
            p0, v0 = min(known, key=lambda kv: abs(kv[1] - target))
            guess = p0 + (target - v0)
            return guess if 1 <= guess <= max(mapping) else None
    return None


def title_on_page(entry: Entry, lines: list[str]) -> bool:
    want = [w for w in _WORD.findall(entry.title.lower()) if w not in {"the", "and", "for", "with"}][:4]
    if not want:
        return False
    top = " ".join(lines[:15]).lower()
    return sum(w in top for w in want) >= max(1, (len(want) + 1) // 2)


def heads_entries(pages: dict[int, list[str]]) -> list[Entry]:
    seen, out = set(), []
    for p in sorted(pages):
        for ln in [x for x in pages[p] if x.strip()][:3]:
            m = _HEAD.match(ln.replace("#", "").replace("*", ""))
            if m and m.group("num") not in seen:
                seen.add(m.group("num"))
                num = m.group("num")
                out.append(Entry(min(3, num.count(".") + 1), num, m.group("title").strip(), "", p, True))
    return out


def build(cfg: Config, con: sqlite3.Connection, sha: str, title: str, path: str) -> dict:
    con.executescript(SCHEMA)
    obj = cfg.objects / sha
    pages = read_pages(obj)
    if not pages:
        return {"sha256": sha, "error": "not extracted"}
    mapping = {p: v for p, v in con.execute("SELECT pdf_page, printed_page FROM pages WHERE sha256=?", (sha,))}
    methods = {p: m for p, m in con.execute("SELECT pdf_page, method FROM pages WHERE sha256=?", (sha,))}
    pdf = cfg.sources / path if path.lower().endswith(".pdf") else None
    entries, source = toc_entries(pdf, pages, methods), "toc"

    toc_end = 0
    for e in entries:
        e.pdf = printed_to_pdf(e.printed, mapping)
        if e.pdf is None:
            continue
        for d in (0, 1, -1, 2, -2):
            q = e.pdf + d
            if q in pages and title_on_page(e, pages[q]):
                e.pdf, e.verified = q, True
                break
    entries = [e for e in entries if e.pdf]
    # keep entries in increasing page order (drop ones that jump backwards: misparsed lines)
    ordered, last = [], 0
    for e in entries:
        if e.pdf >= last:
            ordered.append(e)
            last = e.pdf
    entries = ordered
    if len(entries) < 3 or sum(e.verified for e in entries) < 0.3 * len(entries):
        heads = heads_entries(pages)
        if len(heads) >= 3:
            entries, source = heads, "heads"
    n = max(pages)
    rows = []
    for i, e in enumerate(entries):
        nxt = next((f.pdf for f in entries[i + 1:] if f.level <= e.level), n + 1)
        end = max(e.pdf, nxt - 1)
        rows.append((sha, i, e.level, e.number, e.title, e.printed or mapping.get(e.pdf), e.pdf, end, source,
                     int(e.verified)))
    con.execute("DELETE FROM sections WHERE sha256=?", (sha,))
    con.executemany("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    v = sum(r[9] for r in rows)
    log.info("sections: %s — %d entries from %s, %d verified on page", title, len(rows), source, v)
    return {"sha256": sha, "sections": len(rows), "source": source, "verified": v}


def locate(con: sqlite3.Connection, sha: str, pdf_page: int) -> dict:
    """Innermost section and its chapter for a PDF page."""
    try:
        rows = con.execute("SELECT level, number, title FROM sections WHERE sha256=? AND pdf_start<=? AND pdf_end>=? "
                           "ORDER BY level", (sha, pdf_page, pdf_page)).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not rows:
        return {}
    fmt = lambda r: (f"{r[1]} " if r[1] else "") + r[2]
    chapter = next((r for r in rows if r[0] == 1), rows[0])
    return {"chapter": fmt(chapter), "section": fmt(rows[-1])}
