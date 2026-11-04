"""Audit: is every book indexed *correctly*? One row per book, flags with reasons.

Flags
  not-extracted      no page files yet (pending / seeded / pending-ocr)
  low-body-pages     pages still below quality outside the first/last 5% (front/back matter is ignored)
  damaged-text       text layer with font-encoding damage: private-use / replacement / (cid:) glyphs on
                     >= 3 pages or >= 2% of pages (symbol fonts without a Unicode map listed as evidence)
  few-printed-pages  printed page known for < 50% of pages: citations fall back to PDF pages
  no-sections        no chapters/sections found; few-sections: < 5 for a 100+ page book;
                     sections-unverified: < 50% confirmed on their page
  (pages OCR confirmed as nearly empty - blank versos, part titles - are not low quality)
  not-indexed        extracted but not in the search index, or index older than the extraction

`--mark` adds damaged pages to the book's low pages, so the next `extract --llm-ocr` re-OCRs them.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .config import Config
from .db import now
from .log import log
from .text import is_confirmed_sparse

# Private use area, except U+F8E5-F8FF: the brace/bracket pieces of big delimiters (CMEX, Symbol),
# which are layout, not lost symbols.
PUA = re.compile("[\ue000-\uf8e4\ufffd]|\\(cid:\\d+\\)")
SYMBOL_FONT = re.compile(r"sym|cmsy|cmmi|cmex|msam|msbm|mnsymbol|math|txsy|euler|eus[mb]|stix|mtsy|rsfs|wasy", re.I)
FLAG_ORDER = ["not-extracted", "damaged-text", "low-body-pages", "not-indexed", "few-printed-pages",
              "no-sections", "few-sections", "sections-unverified"]


def damaged_pages(obj: Path) -> list[int]:
    out = []
    for f in sorted((obj / "pages").glob("*.md")):
        if len(PUA.findall(f.read_text(encoding="utf-8", errors="replace"))) >= 2:
            out.append(int(f.stem))
    return out


def unmapped_symbol_fonts(pdf: Path) -> list[str]:
    if not shutil.which("pdffonts") or not pdf.is_file():
        return []
    try:
        r = subprocess.run(["pdffonts", str(pdf)], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return []
    names = set()
    for ln in r.stdout.decode("utf-8", "replace").splitlines()[2:]:
        cols = ln.split()
        if len(cols) >= 6 and cols[-3] == "no":  # 'uni' column
            name = cols[0].split("+", 1)[-1]
            if SYMBOL_FONT.search(name):
                names.add(name)
    return sorted(names)


def _sparse(obj: Path, m: dict, p: int) -> bool:
    info = m["page"].get(str(p), {})
    f = obj / "pages" / f"{p:04d}.md"
    lines = f.read_text(encoding="utf-8").split("\n") if f.is_file() else []
    return is_confirmed_sparse(info.get("method", ""), lines)


def _table(con, name) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())


def audit(cfg: Config, con: sqlite3.Connection, mark: bool = False, fonts: bool = True) -> dict:
    con.row_factory = sqlite3.Row
    has_sections, has_index = _table(con, "sections"), _table(con, "index_state")
    rows, flagged = [], {}
    books = con.execute("SELECT * FROM books WHERE status != 'unsupported' ORDER BY path").fetchall()

    # File work (page scans, pdffonts) in parallel; SQL stays on this thread.
    from concurrent.futures import ThreadPoolExecutor
    import time
    t0 = time.time()

    def scan(b):
        obj = cfg.objects / b["sha256"]
        if not (obj / "extract.json").is_file():
            return b["sha256"], None
        m = json.loads((obj / "extract.json").read_text(encoding="utf-8"))
        n = m["pages"]
        dmg = damaged_pages(obj)
        low = [p for p in m.get("low_quality_pages", []) if not _sparse(obj, m, p)]
        fonts_bad = []
        if fonts and b["ext"] == "pdf" and (len(dmg) >= 3 or (n and len(dmg) >= 0.02 * n)):
            fonts_bad = unmapped_symbol_fonts(cfg.sources / b["path"])
        return b["sha256"], (m, dmg, low, fonts_bad)

    scanned = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (sha, res) in enumerate(pool.map(scan, books), 1):
            scanned[sha] = res
            if i % 50 == 0 or i == len(books):
                log.info("  audit: scanned %d/%d books (%.0fs)", i, len(books), time.time() - t0)

    for b in books:
        sha = b["sha256"]
        obj = cfg.objects / sha
        mpath = obj / "extract.json"
        r = {"title": b["title"], "author": b["author"], "category": b["category"], "status": b["status"],
             "pages": b["pdf_pages"], "path": b["path"], "sha256": sha, "flags": [], "reasons": []}
        def flag(f, why):
            r["flags"].append(f)
            r["reasons"].append(why)
        if scanned.get(sha) is None:
            flag("not-extracted", b["status"])
            rows.append(r)
            continue
        m, dmg, low, fonts_bad = scanned[sha]
        n = m["pages"]
        r["pages"] = n
        r["methods"] = " ".join(f"{k}:{v}" for k, v in sorted(m["methods"].items()))
        edge = max(1, round(0.05 * n))
        body = [p for p in low if edge < p <= n - edge]
        r["low"] = len(low)
        r["ocr_body"] = body
        r["ocr_all"] = low
        if body:
            flag("low-body-pages", f"{len(body)} body pages: {body[:12]}{'…' if len(body) > 12 else ''}")

        r["damaged"] = len(dmg)
        if len(dmg) >= 3 or (n and len(dmg) >= 0.02 * n):
            flag("damaged-text", f"{len(dmg)} pages with unmapped glyphs" +
                 (f"; symbol fonts without Unicode map: {', '.join(fonts_bad[:5])}" if fonts_bad else ""))
            if mark:
                new = sorted(set(low) | set(dmg))
                m["low_quality_pages"] = new
                mpath.write_text(json.dumps(m, indent=1), encoding="utf-8")
                con.execute("UPDATE books SET status='extracted-partial', reason=?, updated_at=? WHERE sha256=?",
                            (f"damaged text on {len(dmg)} pages (audit)", now(), sha))

        printed = con.execute("SELECT count(*) FROM pages WHERE sha256=? AND printed_page IS NOT NULL",
                              (sha,)).fetchone()[0]
        r["printed_pct"] = round(100 * printed / n) if n else 0
        if b["ext"] == "pdf" and n > 20 and r["printed_pct"] < 50:
            flag("few-printed-pages", f"{r['printed_pct']}% of pages have a printed number: add pagemaps/{sha[:16]}.txt")

        if has_sections:
            s = con.execute("SELECT count(*), coalesce(sum(verified),0) FROM sections WHERE sha256=?", (sha,)).fetchone()
            r["sections"], r["sections_verified"] = s[0], s[1]
            if n > 20 and s[0] == 0:
                flag("no-sections", "no contents page or numbered headings found")
            elif n > 100 and s[0] < 5:
                flag("few-sections", f"only {s[0]} entries for {n} pages: contents page probably not parsed")
            elif s[0] and s[1] < 0.5 * s[0]:
                flag("sections-unverified", f"only {s[1]}/{s[0]} confirmed on their page")
        if has_index:
            st = con.execute("SELECT extracted_at, chunks FROM index_state WHERE sha256=?", (sha,)).fetchone()
            r["chunks"] = st[1] if st else 0
            if not st:
                flag("not-indexed", "extracted, not indexed")
            elif st[0] != m["extracted_at"]:
                flag("not-indexed", "index older than extraction")
        else:
            flag("not-indexed", "no index yet")
        rows.append(r)
    if mark:
        con.commit()
    for r in rows:
        for f in r["flags"]:
            flagged[f] = flagged.get(f, 0) + 1
    write(cfg, rows, flagged)
    clean = sum(1 for r in rows if not r["flags"])
    log.info("audit: %d books, %d clean; flags %s", len(rows), clean,
             {f: flagged[f] for f in FLAG_ORDER if f in flagged})
    return {"books": len(rows), "clean": clean, "flags": {f: flagged[f] for f in FLAG_ORDER if f in flagged},
            "report": str(cfg.reports / "audit.md")}


def write(cfg: Config, rows: list[dict], flagged: dict) -> None:
    cfg.reports.mkdir(parents=True, exist_ok=True)
    cell = lambda v: "" if v is None else str(v).replace("|", "\\|")
    md = ["# Librarian audit", "", f"{now()}. {len(rows)} books, "
          f"{sum(1 for r in rows if not r['flags'])} with no flags.", "",
          "| flag | books |", "|---|---:|"]
    md += [f"| {f} | {flagged[f]} |" for f in FLAG_ORDER if f in flagged]
    for f in FLAG_ORDER:
        group = [r for r in rows if f in r["flags"]]
        if not group or f == "not-extracted":
            continue
        md += ["", f"## {f} ({len(group)})", "", "| book | author | pages | detail |", "|---|---|---:|---|"]
        for r in group:
            md.append(f"| {cell(r['title'])} | {cell(r['author'])} | {cell(r['pages'])} | "
                      f"{cell(r['reasons'][r['flags'].index(f)])} |")
    ne = [r for r in rows if "not-extracted" in r["flags"]]
    if ne:
        md += ["", f"## not-extracted ({len(ne)})", "", "Run `scripts\\run.ps1 run --select all` "
               "(text layer) or add no-text-layer books to config/ocr-queue.txt.", ""]
        by = {}
        for r in ne:
            by[r["reasons"][0]] = by.get(r["reasons"][0], 0) + 1
        md += [f"- {k}: {v}" for k, v in sorted(by.items())]
    clean = [r for r in rows if not r["flags"]]
    md += ["", f"## clean ({len(clean)})", ""]
    md += [f"- {r['title']} — {r['author'] or '?'}: {r['pages']} pp, {r.get('printed_pct')}% printed pages, "
           f"{r.get('sections', 0)} sections, {r.get('chunks', 0)} chunks" for r in clean]
    work = sorted((r for r in rows if r.get("ocr_body")), key=lambda r: -len(r["ocr_body"]))
    rate = 11.0  # pages/min with 3 Codex workers, measured on the OCR queue
    with (cfg.reports / "ocr-worklist.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["body_pages", "all_low_pages", "pages", "minutes", "title", "author", "path", "pages_list"])
        for r in work:
            w.writerow([len(r["ocr_body"]), len(r["ocr_all"]), r["pages"], round(len(r["ocr_body"]) / rate, 1),
                        r["title"], r["author"], r["path"], " ".join(map(str, r["ocr_body"]))])
    (cfg.reports / "ocr-worklist.txt").write_text(
        "# Books with low-quality body pages (from audit). Use: run --select @<this file> --llm-ocr --body-only\n"
        + "".join(f"{r['sha256'][:16]}  # {r['title']} - {len(r['ocr_body'])} pages\n" for r in work),
        encoding="utf-8")
    total = sum(len(r["ocr_body"]) for r in work)
    md += ["", f"## OCR worklist: {len(work)} books, {total} body pages, ~{total / rate / 60:.1f} h", "",
           "`reports/ocr-worklist.csv` (per book, with page lists) and `ocr-worklist.txt` "
           "(`run --select @<data>/reports/ocr-worklist.txt --llm-ocr --body-only`).", ""]
    md += [f"- {len(r['ocr_body'])} pages: {r['title']} — {r['author'] or '?'}" for r in work[:25]]
    (cfg.reports / "audit.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    keys = ["title", "author", "category", "status", "pages", "methods", "low", "damaged", "printed_pct",
            "sections", "sections_verified", "chunks", "flags", "reasons", "path", "sha256"]
    with (cfg.reports / "audit.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "flags": " ".join(r["flags"]), "reasons": " | ".join(r["reasons"])})
