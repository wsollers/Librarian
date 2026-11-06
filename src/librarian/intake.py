r"""Ingestion recon (docs/ingestion.md, steps 1-3): what is waiting in the inbox folders, and how
much of it the library already has.

`librarian intake check` looks at every .pdf/.epub/.djvu under the configured inboxes and, cheapest
check first:

1. full SHA-256 (cached per path/size/mtime): a hash the library already has, or a second copy of a
   file seen elsewhere in the inboxes, is an exact duplicate;
2. a quick probe of the rest: pages, text layer, title/author from the metadata or the file name,
   ISBNs (checksum-validated, compared as ISBN-13), DOI, arXiv id, edition statement;
3. hints against the library: an ISBN that a library book prints in its first pages, or a title
   that closely matches a library title.

It writes reports/intake-check.md (summary), reports/intake-check.csv (every file) and
reports/intake-check.json (summary for the MCP server). `check` is read-only.

`librarian intake sort` does the same check and then carries out the dispositions that need no
judgement (dry run unless --apply; every action logged to reports/intake-actions.tsv):
  * exact duplicates (of a library file, of another inbox file, of something filed before) are
    deleted;
  * papers (at most PAPER_PAGES pages) are moved to the papers folder (D:\Readings\Papers) for
    sorting by hand: copied, the copy verified by SHA-256, then the original deleted.
New books and other editions stay in the inbox for the next steps (triage, filing).
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .inventory import probe_pdf, sha256_file, title_from_filename

EXTS = {".pdf", ".epub", ".djvu", ".djv"}
PARTIAL = {".crdownload", ".part", ".partial", ".download", ".tmp"}
MIN_BYTES = 100_000
PAPER_PAGES = 125         # at most this many pages: a paper or notes, moved to the papers folder for sorting by hand
ID_PAGES = 15             # pages read for ISBN / edition / title
TITLE_MATCH = 0.86        # difflib ratio for a "same title" hint

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    verdict     TEXT,
    detail      TEXT              -- JSON: probe results and evidence
);
CREATE INDEX IF NOT EXISTS intake_sha ON intake(sha256);
CREATE TABLE IF NOT EXISTS intake_rejects (   -- hashes decided once (paper, not maths, rejected): never asked again
    sha256      TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    decided_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_done (      -- inbox files dealt with: a reappearing copy is a duplicate
    sha256      TEXT PRIMARY KEY,
    disposition TEXT NOT NULL,                -- paper | hold | filed
    dest        TEXT NOT NULL,
    done_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_ids (         -- identifiers printed in a library book's first pages
    sha256      TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- isbn13 | doi
    value       TEXT NOT NULL,
    PRIMARY KEY (sha256, kind, value)
);
CREATE INDEX IF NOT EXISTS book_ids_value ON book_ids(kind, value);
CREATE TABLE IF NOT EXISTS book_ids_done (sha256 TEXT PRIMARY KEY);
"""

# ---------------------------------------------------------------------------------------------
# identifiers

_ISBN = re.compile(r"\bISBN(?:-1[03])?\s*:?\s*((?:97[89][\s\-]?)?(?:\d[\s\-]?){9}[\dXx])\b"
                   r"|\b(97[89][\-\s]?\d{1,5}[\-\s]\d{1,7}[\-\s]\d{1,7}[\-\s]\d)\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)])")
_ARXIV = re.compile(r"\barXiv:\s?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)
_EDITION = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+edition\b", re.I)
_YEAR = re.compile(r"(?:©|\(c\)|copyright)\s*(?:by\s+)?(?:[^\d\n]{0,160})?((?:19|20)\d\d)", re.I)


def isbn13(raw: str) -> Optional[str]:
    """Validated ISBN-13 for an ISBN-10 or ISBN-13 string, else None."""
    d = re.sub(r"[\s\-]", "", raw).upper()
    if len(d) == 10 and re.fullmatch(r"\d{9}[\dX]", d):
        s = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        if s % 11:
            return None
        core = "978" + d[:9]
    elif len(d) == 13 and d.isdigit() and d[:3] in ("978", "979"):
        if sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d)) % 10:
            return None
        return d
    else:
        return None
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def identifiers(text: str) -> dict:
    isbns = []
    for m in _ISBN.finditer(text):
        v = isbn13(m.group(1) or m.group(2))
        if v and v not in isbns:
            isbns.append(v)
    dois = list(dict.fromkeys(m.group(1).lower() for m in _DOI.finditer(text)))[:5]
    arxiv = next((m.group(1) for m in _ARXIV.finditer(text)), None)
    ed = _EDITION.search(text)
    years = [int(y) for y in _YEAR.findall(text)]
    return {"isbn": isbns[:8], "doi": dois, "arxiv": arxiv,
            "edition": ed.group(0) if ed else None, "year": max(years) if years else None}


def norm_title(t: Optional[str]) -> str:
    t = (t or "").lower()
    t = re.sub(r"\(.*?\)|\b(\d+(st|nd|rd|th)|second|third|revised)\s+(ed(ition)?|edn)\b\.?", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# ---------------------------------------------------------------------------------------------
# library side

def build_book_ids(con: sqlite3.Connection, objects: Path) -> int:
    """ISBNs and DOIs printed in each library book's first pages (cached per book)."""
    done = {r[0] for r in con.execute("SELECT sha256 FROM book_ids_done")}
    todo = [r[0] for r in con.execute("SELECT sha256 FROM books") if r[0] not in done]
    for sha in todo:
        pages = objects / sha / "pages"
        text = []
        if pages.is_dir():
            for p in sorted(pages.glob("*.md"))[:ID_PAGES]:
                try:
                    text.append(p.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        ids = identifiers("\n".join(text))
        rows = [(sha, "isbn13", v) for v in ids["isbn"]] + [(sha, "doi", v) for v in ids["doi"]]
        con.executemany("INSERT OR IGNORE INTO book_ids VALUES (?,?,?)", rows)
        con.execute("INSERT OR IGNORE INTO book_ids_done VALUES (?)", (sha,))
    con.commit()
    return len(todo)


# ---------------------------------------------------------------------------------------------
# candidate side

def is_online_only(path: Path) -> bool:
    """Drive for desktop / OneDrive placeholder: reading it downloads it."""
    attrs = getattr(os.stat(path), "st_file_attributes", 0)
    return bool(attrs & (0x00400000 | 0x00040000 | 0x00001000))   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def discover(roots: Iterable[Path], exclude: Iterable[Path]) -> tuple[list[Path], dict]:
    skipped = {"partial": 0, "small": 0, "excluded": 0}
    excl = [str(e).lower().rstrip("\\/") for e in exclude]
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dl = dirpath.lower()
            if any(dl == e or dl.startswith(e + os.sep) for e in excl):
                skipped["excluded"] += len(filenames)
                dirnames[:] = []
                continue
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                suf = p.suffix.lower()
                if suf in PARTIAL:
                    skipped["partial"] += 1
                    continue
                if suf not in EXTS:
                    continue
                try:
                    if p.stat().st_size < MIN_BYTES:
                        skipped["small"] += 1
                        continue
                except OSError:
                    continue
                out.append(p)
    return out, skipped


def _pdf_meta(path: Path) -> dict:
    if not shutil.which("pdfinfo"):
        return {}
    try:
        r = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {}
    meta = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        k, _, v = line.partition(":")
        if k in ("Title", "Author", "Encrypted") and v.strip():
            meta[k.lower()] = v.strip()
    return meta


def _pdf_text(path: Path, last: int) -> str:
    try:
        r = subprocess.run(["pdftotext", "-q", "-f", "1", "-l", str(last), "-enc", "UTF-8", str(path), "-"],
                           capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _epub(path: Path) -> dict:
    out = {"pages": None, "has_text": True, "text": ""}
    try:
        with zipfile.ZipFile(path) as z:
            opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
            if opf:
                x = z.read(opf).decode("utf-8", "replace")
                t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", x, re.S)
                a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", x, re.S)
                out["title"] = t.group(1).strip() if t else None
                out["author"] = a.group(1).strip() if a else None
                out["text"] = " ".join(re.findall(r"<dc:identifier[^>]*>(.*?)</dc:identifier>", x, re.S))
                out["text"] = re.sub(r"urn:isbn:", "ISBN ", out["text"], flags=re.I)
    except (zipfile.BadZipFile, OSError) as e:
        out["error"] = f"epub: {e}"
    return out


def probe(path: Path) -> dict:
    ext = path.suffix.lower()
    d: dict = {"ext": ext.lstrip(".")}
    if ext == ".pdf":
        pr = probe_pdf(path)
        d.update(pages=pr.pages, has_text=pr.has_text)
        if pr.error:
            d["error"] = pr.error
        meta = _pdf_meta(path)
        d.update({k: v for k, v in meta.items() if k in ("title", "author")})
        if meta.get("encrypted", "no").startswith("yes"):
            d["encrypted"] = True
        text = _pdf_text(path, ID_PAGES) if pr.has_text else ""
    elif ext == ".epub":
        e = _epub(path)
        text = e.pop("text")
        d.update(e)
    else:   # DjVu: page count only; its text layer is read when it is converted (djvu2pdf)
        d["has_text"] = None
        if shutil.which("djvused"):
            try:
                r = subprocess.run(["djvused", "-e", "n", str(path)], capture_output=True, timeout=60)
                d["pages"] = int(r.stdout.strip() or 0) or None
            except (subprocess.TimeoutExpired, ValueError):
                pass
        text = ""
    d.update(identifiers(text))
    ft, fa = title_from_filename(path.name)
    junk = lambda s: not s or len(s) < 4 or re.fullmatch(r"[\w\-]*\d{3,}[\w\-]*|untitled|microsoft word.*", s.strip(), re.I)  # noqa: E731
    d["title_guess"] = d.get("title") if not junk(d.get("title")) else ft
    d["author_guess"] = d.get("author") or fa
    return d


# ---------------------------------------------------------------------------------------------

def check(cfg, con: sqlite3.Connection, roots: Optional[list[Path]] = None, apply: bool = False,
          verbose: bool = True) -> dict:
    con.execute("PRAGMA busy_timeout = 300000")   # an OCR batch may be writing to the index meanwhile
    con.executescript(SCHEMA)
    roots = roots or list(cfg.inbox)
    if not roots:
        raise SystemExit("no inbox folders: set inbox = [...] in config/local.toml (see config/example.toml)")
    say = (lambda *a: print(*a, file=sys.stderr, flush=True)) if verbose else (lambda *a: None)
    t0 = time.time()
    exclude = list(cfg.inbox_exclude) + [cfg.sources, cfg.data] + ([cfg.publish] if cfg.publish else [])
    missing_roots = [str(r) for r in roots if not r.is_dir()]
    files, skipped = discover(roots, exclude)
    say(f"  {len(files)} candidate files in {len(roots) - len(missing_roots)} inbox folder(s)")
    n_ids = build_book_ids(con, cfg.objects)
    if n_ids:
        say(f"  read identifiers from {n_ids} library books")

    lib = {r[0]: r for r in con.execute("SELECT sha256, path, title, author FROM books")}
    lib_isbn: dict[str, list[str]] = {}
    for sha, v in con.execute("SELECT sha256, value FROM book_ids WHERE kind='isbn13'"):
        lib_isbn.setdefault(v, []).append(sha)
    lib_titles = {sha: norm_title(r[2]) for sha, r in lib.items()}
    rejects = dict(con.execute("SELECT sha256, reason FROM intake_rejects").fetchall())
    done = {r[0]: r[1:] for r in con.execute("SELECT sha256, disposition, dest FROM intake_done")}
    cached = {r[0]: r[1:] for r in con.execute("SELECT path, size, mtime_ns, sha256, first_seen, detail FROM intake")}
    now = datetime.now().isoformat(timespec="seconds")

    rows, first_copy, hashed, online = [], {}, 0, 0
    for i, p in enumerate(files, 1):
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p)
        c = cached.get(key)
        if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
            sha, first_seen, detail = c[2], c[3], json.loads(c[4] or "{}")
        else:
            if is_online_only(p):
                online += 1
            try:
                sha = sha256_file(p)
            except OSError as e:
                rows.append({"path": key, "verdict": "unreadable", "detail": {"error": str(e)}, "size": st.st_size})
                continue
            hashed += 1
            first_seen, detail = (c[3] if c else now), {}
        if i % 25 == 0:
            say(f"  {i}/{len(files)} files ({hashed} hashed)")

        ev: dict = {}
        if sha in lib:
            verdict, ev["library"] = "duplicate", lib[sha][1]
        elif sha in done:
            verdict, ev["filed"] = "duplicate", f"{done[sha][0]}: {done[sha][1]}"
        elif sha in first_copy:
            verdict, ev["copy_of"] = "duplicate-in-inbox", first_copy[sha]
        elif sha in rejects:
            verdict, ev["reason"] = "rejected-before", rejects[sha]
        else:
            first_copy[sha] = key
            if not detail.get("probed"):
                detail = probe(p)
                detail["probed"] = True
            pages = detail.get("pages")
            hits = sorted({s for v in detail.get("isbn", []) for s in lib_isbn.get(v, [])})
            nt = norm_title(detail.get("title_guess"))
            best = max(((difflib.SequenceMatcher(None, nt, t).ratio(), s) for s, t in lib_titles.items() if t and nt),
                       default=(0.0, None))
            if detail.get("error") and not pages:
                verdict = "unreadable"
            elif pages and pages <= PAPER_PAGES:
                verdict = "paper"
            elif hits:
                verdict = "isbn-match"
                ev["library"] = [lib[s][1] for s in hits]
            elif best[0] >= TITLE_MATCH:
                verdict = "title-match"
                ev["library"] = lib[best[1]][1]
                ev["similarity"] = round(best[0], 2)
            else:
                verdict = "new"
        con.execute("INSERT INTO intake(path,size,mtime_ns,sha256,first_seen,last_seen,verdict,detail) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                    "mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, last_seen=excluded.last_seen, "
                    "verdict=excluded.verdict, detail=excluded.detail",
                    (key, st.st_size, st.st_mtime_ns, sha, first_seen, now, verdict, json.dumps(detail)))
        rows.append({"path": key, "sha256": sha, "size": st.st_size, "verdict": verdict, "detail": detail, "evidence": ev})
        con.commit()   # one short write per file: never hold the index's write lock while hashing the next one
    # files gone from the inboxes since the last check
    seen = {r["path"] for r in rows}
    gone = [p for p in cached if p not in seen and any(p.lower().startswith(str(r).lower()) for r in roots)]
    con.executemany("DELETE FROM intake WHERE path=?", [(p,) for p in gone])
    con.commit()

    acted = act(cfg, con, rows, now) if apply else {}

    return write_reports(cfg, rows, {
        "checked_at": now, "seconds": round(time.time() - t0, 1),
        "inboxes": [str(r) for r in roots], "missing_inboxes": missing_roots, "skipped": skipped,
        "files": len(rows), "hashed_now": hashed, "downloaded_online_only": online,
        "applied": apply, "actions": acted, "removed_from_inboxes_since_last_check": len(gone)})


def _free_name(folder: Path, name: str) -> Path:
    dest = folder / name
    n = 2
    while dest.exists():
        dest = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return dest


def act(cfg, con: sqlite3.Connection, rows: list[dict], now: str) -> dict:
    """Delete exact duplicates; move papers to the papers folder (copy, verify, delete original)."""
    cfg.reports.mkdir(parents=True, exist_ok=True)
    counts = {"deleted": 0, "papers_moved": 0, "errors": 0}
    with (cfg.reports / "intake-actions.tsv").open("a", encoding="utf-8") as log:
        for r in rows:
            v, src = r["verdict"], Path(r["path"])
            try:
                if v in ("duplicate", "duplicate-in-inbox"):
                    other = r["evidence"].get("library") or r["evidence"].get("copy_of") or r["evidence"].get("filed")
                    src.unlink()
                    log.write(f"{now}\tdelete\t{v}\t{r['sha256']}\t{src}\t{other}\n")
                    counts["deleted"] += 1
                    r["done"] = "deleted"
                elif v == "paper":
                    cfg.papers.mkdir(parents=True, exist_ok=True)
                    dest = _free_name(cfg.papers, src.name)
                    shutil.copy2(src, dest)
                    if sha256_file(dest) != r["sha256"]:
                        dest.unlink()
                        raise OSError("copy did not verify")
                    src.unlink()
                    con.execute("INSERT OR REPLACE INTO intake_done VALUES (?,?,?,?)", (r["sha256"], "paper", str(dest), now))
                    log.write(f"{now}\tmove\tpaper\t{r['sha256']}\t{src}\t{dest}\n")
                    counts["papers_moved"] += 1
                    r["done"] = f"moved to {dest.parent}"
                else:
                    continue
                con.execute("DELETE FROM intake WHERE path=?", (str(src),))
                con.commit()
            except OSError as e:
                counts["errors"] += 1
                r["done"] = f"error: {e}"
                log.write(f"{now}\terror\t{v}\t{r['sha256']}\t{src}\t{e}\n")
    return counts


ORDER = ["new", "title-match", "isbn-match", "duplicate", "duplicate-in-inbox", "rejected-before", "paper", "unreadable"]
MEANING = {
    "new": "not in the library as far as hash, ISBN and title can tell",
    "title-match": "title close to a library book: same book, another edition, or a namesake (fingerprint step decides)",
    "isbn-match": "prints an ISBN that a library book prints: the same book or a sibling edition",
    "duplicate": "byte-identical to a library file",
    "duplicate-in-inbox": "byte-identical to another inbox file",
    "rejected-before": "decided before (paper, not maths, rejected)",
    "paper": f"at most {PAPER_PAGES} pages: goes to the papers folder for sorting by hand",
    "unreadable": "could not be read",
}


def write_reports(cfg, rows: list[dict], summary: dict) -> dict:
    rep = cfg.reports
    rep.mkdir(parents=True, exist_ok=True)
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ORDER}
    summary["by_verdict"] = {k: v for k, v in counts.items() if v}
    summary["new_pages"] = sum((r["detail"].get("pages") or 0) for r in rows if r["verdict"] == "new")
    summary["new_without_text"] = sum(1 for r in rows if r["verdict"] == "new" and r["detail"].get("has_text") is False)

    with (rep / "intake-check.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "path", "sha256", "size_mb", "pages", "text_layer", "title", "author",
                    "isbn", "edition", "year", "library_match", "done"])
        for r in sorted(rows, key=lambda r: (ORDER.index(r["verdict"]), r["path"].lower())):
            d, ev = r["detail"], r.get("evidence", {})
            lm = ev.get("library") or ev.get("copy_of") or ev.get("reason") or ""
            w.writerow([r["verdict"], r["path"], (r.get("sha256") or "")[:16], round(r["size"] / 1e6, 1),
                        d.get("pages") or "", {True: "yes", False: "no"}.get(d.get("has_text"), ""),
                        d.get("title_guess") or "", d.get("author_guess") or "", " ".join(d.get("isbn", [])),
                        d.get("edition") or "", d.get("year") or "", "; ".join(lm) if isinstance(lm, list) else lm,
                        r.get("done", "")])

    L = ["# Intake check", "", f"Checked {summary['checked_at']} in {summary['seconds']} s: "
         f"{summary['files']} files in {len(summary['inboxes']) - len(summary['missing_inboxes'])} inbox folder(s).", ""]
    if summary["missing_inboxes"]:
        L += ["Inbox folders not found: " + ", ".join(f"`{m}`" for m in summary["missing_inboxes"]), ""]
    L += ["| Verdict | Files | Meaning |", "|---|---:|---|"]
    L += [f"| {v} | {counts[v]} | {MEANING[v]} |" for v in ORDER if counts[v]]
    L += ["", f"New books: {counts['new']} ({summary['new_pages']} pages; {summary['new_without_text']} without a text layer, "
          "which need Codex OCR for identification and contents)."]
    if summary["applied"]:
        a = summary["actions"]
        L += [f"Done: {a['deleted']} exact duplicates deleted, {a['papers_moved']} papers moved to the papers folder"
              + (f", {a['errors']} errors (see reports/intake-actions.tsv)" if a["errors"] else "") + "."]
    elif counts["duplicate"] + counts["duplicate-in-inbox"] + counts["paper"]:
        L += [f"`intake sort --apply` would delete {counts['duplicate'] + counts['duplicate-in-inbox']} exact "
              f"duplicates and move {counts['paper']} papers to the papers folder."]
    if summary["downloaded_online_only"]:
        L += [f"{summary['downloaded_online_only']} online-only Drive files were downloaded to hash them."]

    def section(v, title, limit=40):
        sel = [r for r in rows if r["verdict"] == v]
        if not sel:
            return []
        out = ["", f"## {title} ({len(sel)})", ""]
        for r in sorted(sel, key=lambda r: r["path"].lower())[:limit]:
            d, ev = r["detail"], r.get("evidence", {})
            bits = [f"{d['pages']} pp" if d.get("pages") else None,
                    {True: "text", False: "scan"}.get(d.get("has_text")), d.get("edition"),
                    str(d["year"]) if d.get("year") else None]
            lm = ev.get("library")
            line = f"- {d.get('title_guess') or Path(r['path']).name}"
            if d.get("author_guess"):
                line += f" — {d['author_guess']}"
            line += f" ({', '.join(b for b in bits if b)})" if any(bits) else ""
            if lm:
                line += f"  \n  library: {'; '.join(lm) if isinstance(lm, list) else lm}"
                if ev.get("similarity"):
                    line += f" (title {ev['similarity']})"
            line += f"  \n  `{r['path']}`"
            out.append(line)
        if len(sel) > limit:
            out.append(f"- … {len(sel) - limit} more in reports/intake-check.csv")
        return out
    L += section("new", "New books")
    L += section("title-match", "Possible duplicates or other editions (title)")
    L += section("isbn-match", "Possible duplicates (ISBN)")
    L += section("unreadable", "Unreadable")
    L += ["", "Full list: reports/intake-check.csv"]
    (rep / "intake-check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    summary["report"] = str(rep / "intake-check.md")
    (rep / "intake-check.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
