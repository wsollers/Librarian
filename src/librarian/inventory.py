"""Phase 1: inventory every file under the sources root and report coverage.

Incremental: a file whose (size, mtime) is unchanged keeps its cached sha256.
Every distinct book ends with exactly one status, so nothing is silently missing.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import Config
from .db import connect, now, set_meta

SUPPORTED = {"pdf", "djvu", "epub"}

STATUSES = {
    "seeded": "legacy Markdown exists; to be verified against pdfinfo in phase 2",
    "pending": "readable, text layer present, not yet extracted",
    "pending-ocr": "readable, no usable text layer: needs nougat / LLM OCR",
    "unsupported": "file type Librarian does not extract",
    "error": "could not be read",
}



@dataclass
class PdfProbe:
    pages: Optional[int]
    has_text: Optional[bool]
    error: Optional[str]


def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def walk(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def probe_pdf(path: Path) -> PdfProbe:
    if not shutil.which("pdfinfo"):
        return PdfProbe(None, None, "pdfinfo not installed")
    try:
        r = _run(["pdfinfo", str(path)])
    except subprocess.TimeoutExpired:
        return PdfProbe(None, None, "pdfinfo timed out")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return PdfProbe(None, None, "pdfinfo: " + (msg[-1] if msg else f"exit {r.returncode}"))
    m = re.search(rb"^Pages:\s+(\d+)", r.stdout, re.M)
    pages = int(m.group(1)) if m else None
    if not pages:
        return PdfProbe(pages, None, "pdfinfo reports no pages")
    # Sample three interior pages: front matter is often images even in born-digital books.
    samples = sorted({max(1, pages * k // 4) for k in (1, 2, 3)})
    chars = 0
    for p in samples:
        try:
            t = _run(["pdftotext", "-q", "-f", str(p), "-l", str(p), "-layout", str(path), "-"], 30)
        except subprocess.TimeoutExpired:
            continue
        chars += sum(1 for c in t.stdout.decode("utf-8", "replace") if c.isalpha())
    return PdfProbe(pages, chars >= 200 * len(samples), None)


def load_manifest(path: Optional[Path]) -> dict[str, dict]:
    if not path or not path.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        print("note: PyYAML not installed; manifest titles skipped", file=sys.stderr)
        return {}
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out = {}
    for s in doc.get("sources", []) or []:
        sha = (s.get("sha256") or "").lower()
        if sha:
            out[sha] = {"title": s.get("canonical_title"), "author": s.get("author")}
    return out


_EDITION = re.compile(r"^(\d+|\d+(st|nd|rd|th)( ed(ition)?)?|vol(ume)?\.? ?\d+|copy)$", re.I)


def title_from_filename(rel: str) -> tuple[str, Optional[str]]:
    """'Title - 6th - Author.pdf', 'Title - Author - 2.pdf' -> (title [+ edition], author)."""
    parts = [p.strip() for p in Path(rel).stem.split(" - ") if p.strip()]
    tags = [p for p in parts[1:] if _EDITION.match(p)]
    rest = [p for p in parts if p not in tags or p == parts[0]]
    if len(rest) < 2:
        return " - ".join(parts), None
    title = " - ".join(rest[:-1])
    ed = [t for t in tags if not t.isdigit() and t.lower() != "copy"]
    return (f"{title} ({', '.join(ed)})" if ed else title), rest[-1]


def run(cfg: Config, *, rehash: bool = False, verbose: bool = True) -> dict:
    root = cfg.sources
    if not root.is_dir():
        raise SystemExit(f"sources root not found: {root}")
    con = connect(cfg.db_path)
    cached = {
        row[0]: row[1:]
        for row in con.execute("SELECT path, sha256, size, mtime_ns FROM files")
    }
    manifest = load_manifest(cfg.manifest)
    legacy_md = cfg.legacy / "library" / "markdown" if cfg.legacy else None
    legacy_obj = cfg.legacy / "objects" / "pdf" if cfg.legacy else None

    seen: set[str] = set()
    by_sha: dict[str, list[tuple[str, int, str]]] = {}
    hashed = 0
    stamp = now()
    for i, path in enumerate(walk(root), 1):
        rel = path.relative_to(root).as_posix()
        st = path.stat()
        ext = path.suffix.lower().lstrip(".")
        prev = cached.get(rel)
        if prev and not rehash and prev[1] == st.st_size and prev[2] == st.st_mtime_ns:
            sha = prev[0]
        else:
            sha = sha256_file(path)
            hashed += 1
        con.execute(
            "INSERT INTO files(path, sha256, size, mtime_ns, ext, seen_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, size=excluded.size, "
            "mtime_ns=excluded.mtime_ns, ext=excluded.ext, seen_at=excluded.seen_at",
            (rel, sha, st.st_size, st.st_mtime_ns, ext, stamp),
        )
        seen.add(rel)
        by_sha.setdefault(sha, []).append((rel, st.st_size, ext))
        if i % 50 == 0:
            con.commit()  # long first run: keep partial progress
        if verbose and i % 50 == 0:
            print(f"  {i} files ({hashed} hashed)", file=sys.stderr)
    gone = set(cached) - seen
    con.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])

    existing = {
        r[0]: r[1:]
        for r in con.execute("SELECT sha256, pdf_pages, has_text, status, reason FROM books")
    }
    con.execute("DELETE FROM books WHERE sha256 NOT IN (SELECT sha256 FROM files)")
    for n, (sha, entries) in enumerate(by_sha.items(), 1):
        if n % 25 == 0:
            con.commit()
        rel, size, ext = sorted(entries)[0]
        meta = manifest.get(sha, {})
        ftitle, fauthor = title_from_filename(rel)
        # Filenames are curated; legacy manifest titles are often series names or PDF metadata junk.
        title = ftitle
        author = meta.get("author") if (fauthor and meta.get("author") and fauthor.lower() in meta["author"].lower()) else fauthor
        category = rel.split("/", 1)[0] if "/" in rel else None
        seed_md = legacy_md / f"LIB-{sha[:16]}.md" if legacy_md else None
        seed_md = seed_md.relative_to(cfg.legacy).as_posix() if seed_md and seed_md.is_file() else None
        seed_obj = legacy_obj / sha if legacy_obj else None
        seed_obj = seed_obj.relative_to(cfg.legacy).as_posix() if seed_obj and seed_obj.is_dir() else None

        pages = has_text = reason = None
        if ext not in SUPPORTED:
            status, reason = "unsupported", f".{ext} file"
        elif ext == "pdf":
            prev = existing.get(sha)
            if prev and prev[0] and prev[1] is not None and not rehash:
                pages, has_text = prev[0], bool(prev[1])
                probe_err = None
            else:
                pr = probe_pdf(root / rel)
                pages, has_text, probe_err = pr.pages, pr.has_text, pr.error
            if probe_err:
                status, reason = "error", probe_err
            elif seed_md:
                status = "seeded"
            elif has_text:
                status = "pending"
            else:
                status, reason = "pending-ocr", "sampled pages have no text layer"
        elif ext == "djvu":
            status = "seeded" if seed_md else "pending"
            from .extract import djvu_tool
            if not djvu_tool("djvutxt"):
                reason = "djvulibre (djvutxt) not installed"
        else:  # epub
            status = "seeded" if seed_md else "pending"
            reason = "extract by section; no PDF pages"

        con.execute(
            "INSERT INTO books(sha256, path, ext, size, title, author, category, pdf_pages, has_text,"
            " seed_markdown, seed_objects, status, reason, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sha256) DO UPDATE SET path=excluded.path, ext=excluded.ext, size=excluded.size,"
            " title=excluded.title, author=excluded.author, category=excluded.category,"
            " pdf_pages=excluded.pdf_pages, has_text=excluded.has_text,"
            " seed_markdown=excluded.seed_markdown, seed_objects=excluded.seed_objects,"
            " status=CASE WHEN books.status IN ('extracted','extracted-partial','indexed') THEN books.status ELSE excluded.status END,"
            " reason=excluded.reason, updated_at=excluded.updated_at",
            (sha, rel, ext, size, title, author, category, pages,
             None if has_text is None else int(has_text), seed_md, seed_obj, status, reason, stamp),
        )
    set_meta(con, "inventory_at", stamp)
    set_meta(con, "sources_root", str(root))
    con.commit()

    summary = report(con, cfg)
    summary.update(files=len(seen), hashed=hashed, removed=len(gone))
    con.close()
    return summary


def report(con: sqlite3.Connection, cfg: Config) -> dict:
    cfg.reports.mkdir(parents=True, exist_ok=True)
    rows = con.execute(
        "SELECT status, category, title, author, ext, pdf_pages, has_text, reason, path, sha256,"
        " seed_objects IS NOT NULL FROM books ORDER BY status, category, title"
    ).fetchall()
    dups = con.execute(
        "SELECT sha256, group_concat(path, ' | ') FROM files GROUP BY sha256 HAVING count(*) > 1"
    ).fetchall()
    counts = Counter(r[0] for r in rows)
    files_n = con.execute("SELECT count(*) FROM files").fetchone()[0]
    stamp = con.execute("SELECT value FROM meta WHERE key='inventory_at'").fetchone()[0]

    md = [
        "# Librarian coverage report", "",
        f"Inventory: {stamp}. Sources: `{cfg.sources}`.", "",
        f"{files_n} files, {len(rows)} distinct books, {len(dups)} duplicate groups.", "",
        "| status | books | meaning |", "|---|---:|---|",
    ]
    for s, meaning in STATUSES.items():
        md.append(f"| {s} | {counts.get(s, 0)} | {meaning} |")
    for s in counts:
        if s not in STATUSES:
            md.append(f"| {s} | {counts[s]} | |")
    for s in list(STATUSES) + [s for s in counts if s not in STATUSES]:
        group = [r for r in rows if r[0] == s]
        if not group:
            continue
        md += ["", f"## {s} ({len(group)})", "",
               "| category | title | author | type | pages | text | legacy cache | reason |",
               "|---|---|---|---|---:|---|---|---|"]
        for r in group:
            text = "" if r[6] is None else ("yes" if r[6] else "no")
            cell = lambda v: "" if v is None else str(v).replace("|", "\\|")
            md.append(f"| {cell(r[1])} | {cell(r[2])} | {cell(r[3])} | {r[4]} | {cell(r[5])} | {text} |"
                      f" {'yes' if r[10] else ''} | {cell(r[7])} |")
    if dups:
        md += ["", f"## duplicates ({len(dups)})", ""]
        md += [f"- `{sha[:12]}`: {paths}" for sha, paths in dups]
    (cfg.reports / "coverage.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with (cfg.reports / "coverage.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "category", "title", "author", "ext", "pdf_pages", "has_text",
                    "reason", "path", "sha256", "legacy_objects"])
        w.writerows(rows)
    return {"books": len(rows), "duplicates": len(dups), "status": dict(counts),
            "report": str(cfg.reports / "coverage.md")}
