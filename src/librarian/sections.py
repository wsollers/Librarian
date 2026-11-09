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
CREATE TABLE IF NOT EXISTS book_parts (   -- where the body is: before it front matter, after it back matter
    sha256      TEXT PRIMARY KEY,
    body_start  INTEGER NOT NULL,          -- PDF page
    body_end    INTEGER NOT NULL,          -- PDF page
    source      TEXT NOT NULL              -- toc | heads | numbering
);
"""

_WORDNUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_RANGE = r"(?:\s*[\u2013\u2014-]\s*\d{1,4})?"
_PAGE_TAIL = re.compile(
    r"^(?P<title>.*?\S)\s*(?:(?:\.\s?){2,}|\s{2,}|\s(?=[0-9ivxlc]+" + _RANGE + r"\s*$))\s*"
    r"(?P<page>\d{1,4}|[ivxlc]{1,7})" + _RANGE + r"\s*$", re.I)
_TRAILING_NUM = re.compile(r"\s(\d{1,4}" + _RANGE + r")\s*$")  # one trailing number column
MAX_TOC_LINE = 300
_HEADING_ONLY = re.compile(r"^(?:chapter|part|appendix|book)\s+([0-9]+|[ivxlc]+|[a-z]+)\b[.:]?\s*\S", re.I)
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


def normalise_toc_line(line: str) -> str:
    line = line.translate(_LIGATURES).replace("\u3000", "  ").replace("\uff0e", ".").replace("\u2002", " ")
    line = line.replace("~~", "").replace("*", "").replace("\u00a0", " ").replace("\t", "    ")
    # Long runs of spaces (layout mode pads columns) only matter as 'a gap': keep three.
    line = re.sub(r" {4,}", "   ", line)
    return line.rstrip()


def strip_number_columns(title: str) -> str:
    """Drop trailing number columns ('Theorems' before 'Pages'), keeping some words."""
    for _ in range(4):
        m = _TRAILING_NUM.search(title)
        if not m:
            break
        rest = title[:m.start()].rstrip(" .")
        if not re.search(r"[A-Za-z]{3}", rest):
            break
        title = rest
    return title


def parse_toc_lines(lines: list[str]) -> list["Entry"]:
    """Line-level parse with two layouts handled across lines:
    a title that wraps (numbered line without a page, continued on the next line), and a chapter
    heading without a page (takes the page of the first entry after it)."""
    out: list[Entry] = []
    carry = ""            # wrapped title waiting for its continuation
    pending_heads: list[Entry] = []
    for raw in lines:
        line = normalise_toc_line(raw)
        if not line.strip():
            continue
        e = parse_entry((carry + " " + line.strip()) if carry else line)
        if e is None and carry:
            e = parse_entry(line)
        if e:
            carry = ""
            for h in pending_heads:
                h.printed = e.printed
                out.append(h)
            pending_heads = []
            out.append(e)
            continue
        txt = line.strip()
        if _HEADING_ONLY.match(txt):
            h = parse_entry(txt + "  0")
            if h:
                h.title = re.sub(r"\s+", " ", h.title).strip()
                pending_heads.append(h)
            carry = ""
        elif _NUM.match(txt) and re.search(r"[A-Za-z]{3}", txt) and len(txt) < 120:
            carry = txt  # numbered entry whose page is on the next line
        elif carry and len(carry) < 200 and pending_heads == []:
            carry = carry + " " + txt
        elif pending_heads and not _NUM.match(txt) and txt.isupper():
            pending_heads[-1].title += " " + txt  # heading wrapped over lines
        else:
            carry = ""
    return out


def parse_entry(line: str) -> Optional[Entry]:
    line = normalise_toc_line(line)
    if len(line) > MAX_TOC_LINE:  # garbled or a paragraph, not a contents entry
        return None
    raw = line.replace("|", "   ").strip()
    m = _PAGE_TAIL.match(raw)
    if not m:
        return None
    title, page = m.group("title"), m.group("page")
    title = strip_number_columns(title).strip(" .|*#")
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
    """Parse the contents pages in the first fifth of the book: runs start at a 'Contents' heading
    with >= 3 entries or any page with >= 6, continue while pages keep parsing (>= 3 entries);
    the longest run wins."""
    n = max(pages) if pages else 0
    scan = range(1, max(1, min(n, 60, n // 5 + 10)) + 1)
    parsed: dict[int, tuple[list[Entry], bool]] = {}
    for p in scan:
        use_layout = pdf is not None and methods.get(p) == "textlayer"
        lines = layout_text(pdf, p).splitlines() if use_layout else pages.get(p, [])
        heading = any(_CONTENTS.match(normalise_toc_line(ln)) for ln in lines[:6])
        parsed[p] = (parse_toc_lines(lines), heading)
    starts = [p for p in scan if (parsed[p][1] and len(parsed[p][0]) >= 3) or len(parsed[p][0]) >= 6]

    def run_from(start: int) -> list[Entry]:
        out: list[Entry] = []
        for p in range(start, scan[-1] + 1):
            if p > start and len(parsed[p][0]) < 3:
                break
            out += parsed[p][0]
        return out

    # The contents is the longest run of entry-dense pages; a preface or a handbook overview
    # with a few numbered lines makes a short run and loses.
    runs = [run_from(p) for p in starts]
    return max(runs, key=len) if runs else []


def codex_toc(obj: Path) -> list[Entry]:
    """Contents read by Codex (librarian toc) for books whose contents pages the parser cannot read:
    objects/<sha>/toc.json, [{"level", "number", "title", "page"}, ...]."""
    import json
    f = obj / "toc.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for d in data.get("entries", []):
        t, pg = str(d.get("title") or "").strip(), str(d.get("page") or "").strip()
        if not t:
            continue
        try:
            lv = max(0, min(3, int(d.get("level", 1))))
        except (TypeError, ValueError):
            lv = 1
        num = d.get("number")
        out.append(Entry(lv, str(num).strip() if num not in (None, "") else None, t[:200], pg.lower()))
    return out


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


def heading_in(entry: Entry, lines: list[str]) -> bool:
    """A heading anywhere on the page: a line starting with the entry's number (or its last part,
    "3." for 19.3) whose words, with the next line, match the title."""
    num = (entry.number or "").strip()
    keys = {num, num.split(".")[-1]} - {""}
    want = [w for w in _WORD.findall(entry.title.lower()) if w not in {"the", "and", "for", "with", "of"}][:4]
    if not want:
        return False
    for i, ln in enumerate(lines):
        t = ln.replace("#", "").replace("*", "").strip()
        if keys and not any(re.match(re.escape(k) + r"[.)]?\s", t) for k in keys):
            continue
        txt = (t + " " + (lines[i + 1] if i + 1 < len(lines) else "")).lower()
        if sum(w in txt for w in want) >= max(1, (len(want) + 1) // 2):
            return True
    return False


def place_unpaged(entries: list[Entry], pages: dict[int, list[str]]) -> None:
    """Entries the contents gives without a page (sections run together in a paragraph): look for
    the heading between the neighbouring located entries."""
    last = 0
    for i, e in enumerate(entries):
        if e.pdf:
            last = e.pdf
            continue
        if e.printed:
            continue
        hi = next((f.pdf for f in entries[i + 1:] if f.pdf), max(pages) if pages else 0)
        for q in range(max(last, 1), hi + 1):
            if q in pages and heading_in(e, pages[q]):
                e.pdf, e.verified, last = q, True, q
                break


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
    entries = codex_toc(obj)
    source = "codex" if entries else "toc"
    if not entries:
        entries = toc_entries(pdf, pages, methods)

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
    if source == "codex":
        place_unpaged(entries, pages)
    entries = [e for e in entries if e.pdf]
    # keep entries in increasing page order (drop ones that jump backwards: misparsed lines)
    ordered, last = [], 0
    for e in entries:
        if e.pdf >= last:
            ordered.append(e)
            last = e.pdf
    entries = ordered
    if source != "codex" and (len(entries) < 3 or sum(e.verified for e in entries) < 0.3 * len(entries)):
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
    parts = body_range(rows, mapping, n, source)
    con.execute("DELETE FROM book_parts WHERE sha256=?", (sha,))
    if parts:
        con.execute("INSERT INTO book_parts VALUES(?,?,?,?)", (sha, *parts))
    con.commit()
    v = sum(r[9] for r in rows)
    log.info("sections: %s — %d entries from %s, %d verified on page", title, len(rows), source, v)
    return {"sha256": sha, "sections": len(rows), "source": source, "verified": v}


_BACK = re.compile(r"^(bibliography|references|index|subject index|name index|author index|index of (symbols|notation|names)"
                   r"|glossary|list of symbols|further reading|selected bibliography)\b", re.I)
_CHAPTER_NO = re.compile(r"^\d+$|^[IVXLC]+$")


def body_range(rows: list[tuple], mapping: dict, n: int, source: str) -> Optional[tuple[int, int, str]]:
    """(first, last PDF page of the body, how it was found). The body starts at the first numbered
    chapter and ends before the first unnumbered back-matter entry after the last numbered one
    (bibliography, index, ...); appendices count as body. Without numbered chapters: where the
    printed numbering switches from roman to arabic 1."""
    chapters = [r for r in rows if r[2] <= 2 and r[3] and _CHAPTER_NO.match(str(r[3]).split(".")[0])]
    if chapters:
        start = min(r[6] for r in chapters)
        last = max(r[6] for r in chapters)
        back = [r[6] for r in rows if r[6] > last and not r[3] and r[2] <= 1 and _BACK.match(r[4].strip())]
        end = (min(back) - 1) if back else n
        return start, max(start, end), source
    arabic = sorted(p for p, v in mapping.items() if v and str(v).isdigit() and int(v) <= 3)
    if arabic:
        return arabic[0], n, "numbering"
    return None


def parts_of(con: sqlite3.Connection, sha: str) -> Optional[tuple[int, int]]:
    try:
        r = con.execute("SELECT body_start, body_end FROM book_parts WHERE sha256=?", (sha,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return (r[0], r[1]) if r else None


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
