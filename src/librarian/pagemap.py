"""Printed page per PDF page.

Detection: page-number candidates in the first/last lines of each page vote for an offset
(pdf_page - printed). A page takes the majority offset in a window around it, so offsets may
drift within a book (plates, inserted pages). Roman front matter is handled the same way.

Overrides (in git, no copyrighted content): pagemaps/<sha16>.txt, one rule per line:
    30-330 +13        PDF pages 30..330 are printed (pdf - 13)
    1-12 roman 1      PDF pages 1..12 are printed i, ii, ...
    13-29 none        no printed number
Overrides win over detection.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from .config import Config
from .log import log

REPO_PAGEMAPS = Path(__file__).resolve().parents[2] / "pagemaps"
_NUM_EDGE = re.compile(r"^\s*(\d{1,4})\b|\b(\d{1,4})\s*$")
_ROMAN_EDGE = re.compile(r"^\s*([ivxlc]{1,7})\b|\b([ivxlc]{1,7})\s*$")
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
WINDOW = 8
MIN_VOTES = 3


def roman_to_int(s: str) -> Optional[int]:
    s = s.lower()
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN.get(ch)
        if v is None:
            return None
        total += -v if v < prev else v
        prev = max(prev, v)
    return total if int_to_roman(total) == s else None


def int_to_roman(n: int) -> str:
    out = ""
    for v, s in ((100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += s
            n -= v
    return out


def edge_lines(lines: list[str], k: int = 3) -> list[str]:
    nb = [ln for ln in lines if ln.strip()]
    return nb[:k] + nb[-k:] if len(nb) > 2 * k else nb


def candidates(pdf_page: int, lines: list[str]) -> tuple[set[int], set[int]]:
    arabic, roman = set(), set()
    for ln in edge_lines(lines):
        if len(ln) > 90:
            continue
        for m in _NUM_EDGE.finditer(ln):
            n = int(m.group(1) or m.group(2))
            if 0 < n <= pdf_page + 50:
                arabic.add(pdf_page - n)
        for m in _ROMAN_EDGE.finditer(ln):
            r = roman_to_int(m.group(1) or m.group(2))
            if r:
                roman.add(pdf_page - r)
    return arabic, roman


def detect(pages: dict[int, list[str]]) -> dict[int, Optional[str]]:
    n = max(pages) if pages else 0
    cand = {p: candidates(p, pages.get(p, [])) for p in range(1, n + 1)}
    out: dict[int, Optional[str]] = {}
    for p in range(1, n + 1):
        lo, hi = max(1, p - WINDOW), min(n, p + WINDOW)
        ar = Counter(o for q in range(lo, hi + 1) for o in cand[q][0])
        ro = Counter(o for q in range(lo, hi + 1) for o in cand[q][1])
        best = None
        if ar:
            off, votes = ar.most_common(1)[0]
            if votes >= MIN_VOTES and p - off >= 1:
                best = str(p - off)
        if best is None and ro:
            off, votes = ro.most_common(1)[0]
            if votes >= MIN_VOTES and p - off >= 1:
                best = int_to_roman(p - off)
        out[p] = best
    return out


def load_override(sha: str, root: Path = REPO_PAGEMAPS) -> list[tuple[int, int, str, int]]:
    path = root / f"{sha[:16]}.txt"
    if not path.is_file():
        return []
    rules = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        m = re.match(r"^(\d+)-(\d+)\s+(?:([+-]\d+)|roman\s+(\d+)|(none))$", ln)
        if not m:
            raise ValueError(f"{path}: bad rule {ln!r}")
        a, b = int(m.group(1)), int(m.group(2))
        if m.group(3):
            rules.append((a, b, "offset", int(m.group(3))))
        elif m.group(4):
            rules.append((a, b, "roman", int(m.group(4))))
        else:
            rules.append((a, b, "none", 0))
    return rules


def apply_override(mapping: dict[int, Optional[str]], rules) -> dict[int, Optional[str]]:
    for a, b, kind, v in rules:
        for p in range(a, b + 1):
            if p in mapping:
                mapping[p] = (str(p - v) if kind == "offset" else
                              int_to_roman(v + p - a) if kind == "roman" else None)
    return mapping


def read_pages(obj: Path) -> dict[int, list[str]]:
    return {int(f.stem): f.read_text(encoding="utf-8").split("\n")[:-1] or []
            for f in sorted((obj / "pages").glob("*.md"))}


def map_book(cfg: Config, con: sqlite3.Connection, sha: str, title: str) -> dict:
    obj = cfg.objects / sha
    pages = read_pages(obj)
    if not pages:
        return {"sha256": sha, "error": "not extracted"}
    mapping = detect(pages)
    rules = load_override(sha)
    if rules:
        mapping = apply_override(mapping, rules)
    con.executemany("UPDATE pages SET printed_page=? WHERE sha256=? AND pdf_page=?",
                    [(v, sha, p) for p, v in mapping.items()])
    con.commit()
    mapped = sum(1 for v in mapping.values() if v)
    offsets = Counter(p - int(v) for p, v in mapping.items() if v and v.isdigit())
    log.info("pagemap: %s — %d/%d pages mapped, offsets %s%s", title, mapped, len(mapping),
             dict(offsets.most_common(3)), " (override)" if rules else "")
    write_book_md(obj, pages, mapping, title)
    return {"sha256": sha, "mapped": mapped, "pages": len(mapping), "override": bool(rules),
            "offsets": dict(offsets.most_common(3))}


def write_book_md(obj: Path, pages: dict[int, list[str]], mapping: dict, title: str) -> None:
    """Whole book as one Markdown file with explicit page breaks, for reading and grepping."""
    out = [f"# {title}", ""]
    for p in sorted(pages):
        out += ["", f"<!-- page pdf={p} printed={mapping.get(p) or '-'} -->", ""]
        out += pages[p]
    (obj / "book.md").write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
