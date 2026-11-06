r"""librarian ingest / librarian store: new books from the inbox folders into the library.

  librarian ingest                 # sort the inboxes (exact duplicates deleted, papers to the papers folder),
                                   # triage what is left with Codex, write the plan and reports/ingest.md
  librarian ingest set 7 --subject "Foundations/Set Theory"      # adjust an item, re-render the report
  librarian ingest set 7 --hold | --library | --edition | --delete | --title T | --author A
  librarian store                  # dry run: what store would do
  librarian store --apply          # file everything as planned, update tags and lists, queue the OCR

Nothing is moved by `ingest` except what `intake sort` already does (exact duplicates, papers).
The plan is the source of truth: reports/ingest-plan.yaml (generated from the triage, kept in the
index, plus every `ingest set` override); reports/ingest.md is rendered from it, with links.

Triage asks Codex, per book, for: library (maths, physics and other science, theoretical computer
science) or hold (everything else); title, author, edition, year; the subject tags of
config/subjects.yaml; and, when the inbox check found a library book with the same ISBN or a close
title, whether it is the same work. Decisions:
  library  -> Sources/<Area>/<Subject>/<Title - Author>.<ext>, tags added to config/subjects.yaml
  edition  -> as library, and listed in config/lower-priority.txt (processed last)
  hold     -> the hold folder (D:\Readings\Hold)
  delete   -> worse copy of a library book (same ISBN, a scan where the library copy has text)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import intake, subjects
from .inventory import sha256_file

REPO = Path(__file__).resolve().parents[2]
CANDIDATES = ("new", "title-match", "isbn-match")
TEXT_PAGES = 14            # pages of text in the dossier
TEXT_CHARS = 24_000
SCAN_IMAGES = 8            # page images for a book without a text layer
TRIAGE_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_triage (
    sha256   TEXT PRIMARY KEY,
    version  INTEGER NOT NULL,
    result   TEXT NOT NULL,     -- JSON from Codex
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_overrides (
    sha256   TEXT PRIMARY KEY,
    fields   TEXT NOT NULL,     -- JSON: fields set with `ingest set`
    at       TEXT NOT NULL
);
"""

PROMPT = """You are sorting new books for a personal mathematics library. Read dossier.md in the current
directory{images}. Reply with ONE JSON object and nothing else:

{{
  "category": "math" | "science" | "cs-theory" | "other",
  "is_book": true | false,
  "title": "the book's title as printed, without edition",
  "author": "author(s) as printed",
  "author_surnames": "surname(s) for a file name: 'Halmos', 'Burris and Sankappanavar', 'Hardy et al'",
  "edition": "e.g. 2nd" or null,
  "year": 2009 or null,
  "same_work_as_library_match": true | false | null,
  "primary": "Area/Subject",
  "secondary": ["Area/Subject", ...],
  "level": "school" | "intro-undergrad" | "undergrad" | "graduate" | "research" | "general",
  "kind": "textbook" | "monograph" | "lecture-notes" | "problem-book" | "solutions" | "reference" | "survey" | "popular" | "other",
  "confidence": "high" | "medium" | "low",
  "reason": "one short sentence: what the book is and why this category and subject"
}}

Rules:
* category: "math" for mathematics; "science" for physics, statistics, mathematical economics and
  other quantitative science; "cs-theory" for algorithms, complexity, logic, semantics, type theory,
  formal methods. "other" for everything else: programming-language and software-engineering books,
  security, IT, AI products and policy, engineering handbooks, business, general reading.
* is_book: false for slides, a thesis, a single paper, a manual or a catalogue.
* same_work_as_library_match: only when the dossier names a library match: true if it is the same
  work (any edition), false if it is a different book with a similar title; otherwise null.
* primary and secondary (0 to 3) must be taken exactly from this list; leave secondary empty if unsure.
  Use primary even for category "other" (closest subject), it is ignored there.
{subjects}
"""


# ---------------------------------------------------------------------------------------------
# dossier

def _pdf_text(path: Path) -> str:
    try:
        r = subprocess.run(["pdftotext", "-q", "-f", "1", "-l", str(TEXT_PAGES), "-enc", "UTF-8", str(path), "-"],
                           capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _epub_text(path: Path) -> str:
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
            for n in names:
                html = z.read(n).decode("utf-8", "replace")
                text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", html))
                out.append(re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip())
                if sum(map(len, out)) > TEXT_CHARS:
                    break
    except (zipfile.BadZipFile, OSError):
        pass
    return "\n\n".join(out)


def _djvu_text(path: Path) -> str:
    if not shutil.which("djvutxt"):
        return ""
    try:
        r = subprocess.run(["djvutxt", f"--page=1-{TEXT_PAGES}", str(path)], capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return ""


def _render(pdf: Path, out: Path, n: int) -> list[Path]:
    imgs = []
    for p in range(1, n + 1):
        prefix = out / f"page{p:02d}"
        try:
            subprocess.run(["pdftoppm", "-q", "-r", "110", "-f", str(p), "-l", str(p), "-png", "-singlefile",
                            str(pdf), str(prefix)], check=True, timeout=120)
            imgs.append(prefix.with_suffix(".png"))
        except (subprocess.SubprocessError, FileNotFoundError):
            break
    return imgs


def dossier(row: dict, twin: Optional[dict]) -> tuple[str, bool]:
    """Markdown for Codex, and whether page images are needed (no usable text)."""
    p = Path(row["path"])
    d = row["detail"]
    ext = p.suffix.lower()
    text = {".pdf": _pdf_text, ".epub": _epub_text, ".djvu": _djvu_text, ".djv": _djvu_text}.get(ext, lambda _: "")(p)
    text = re.sub(r"\n{3,}", "\n\n", text)[:TEXT_CHARS]
    alpha = sum(c.isalpha() for c in text)
    L = [f"# File: {p.name}", "",
         f"- pages: {d.get('pages')}", f"- metadata title: {d.get('title')}", f"- metadata author: {d.get('author')}",
         f"- ISBNs printed: {', '.join(d.get('isbn') or []) or 'none found'}",
         f"- edition statement: {d.get('edition')}", f"- copyright year: {d.get('year')}"]
    if twin:
        L += [f"- library match ({twin['why']}): \"{twin['title']}\" by {twin['author']} ({twin['path']})"]
    L += ["", "## Text of the first pages", "", text if alpha > 300 else "(no usable text: see the page images)"]
    return "\n".join(L), alpha <= 300 and ext == ".pdf"


def codex_json(prompt: str, files: dict[str, str], images: list[Path], timeout: int = 600) -> Optional[dict]:
    exe = shutil.which(os.environ.get("LIBRARIAN_CODEX", "codex"))
    if not exe:
        raise SystemExit("codex CLI not found on PATH")
    with tempfile.TemporaryDirectory() as td:
        for name, content in files.items():
            Path(td, name).write_text(content, encoding="utf-8")
        reply = Path(td) / "reply.txt"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-C", td,
               "--output-last-message", str(reply)]
        for img in images:
            cmd += ["--image", str(img)]
        model = os.environ.get("LIBRARIAN_CODEX_MODEL")
        if model:
            cmd += ["--model", model]
        try:
            subprocess.run(cmd + ["-"], input=prompt.encode("utf-8"), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if not reply.is_file():
            return None
        text = reply.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------------------------
# triage

def _subject_list() -> str:
    tax = subjects.taxonomy()
    return "\n".join(f"  {area}: " + "; ".join(f"{area}/{s}" for s in subs) for area, subs in tax.items())


def triage_one(row: dict, twin: Optional[dict], subject_list: str) -> Optional[dict]:
    md, need_images = dossier(row, twin)
    images: list[Path] = []
    with tempfile.TemporaryDirectory() as td:
        if need_images:
            images = _render(Path(row["path"]), Path(td), SCAN_IMAGES)
        prompt = PROMPT.format(subjects=subject_list,
                               images=" and the attached images of its first pages" if images else "")
        res = codex_json(prompt, {"dossier.md": md}, images)
    if res:
        res["read_from"] = "images" if images else "text"
    return res


def _twin(con: sqlite3.Connection, r: dict) -> Optional[dict]:
    ev = r.get("evidence") or {}
    lib = ev.get("library")
    if r["verdict"] not in ("title-match", "isbn-match") or not lib:
        return None
    path = lib[0] if isinstance(lib, list) else lib
    b = con.execute("SELECT sha256, title, author, has_text, pdf_pages FROM books WHERE path=?", (path,)).fetchone()
    return {"path": path, "sha256": b[0] if b else None, "title": b[1] if b else path, "author": b[2] if b else "?",
            "has_text": bool(b[3]) if b else None, "pages": b[4] if b else None,
            "why": "same ISBN" if r["verdict"] == "isbn-match" else f"similar title {ev.get('similarity', '')}"}


def _candidates(con: sqlite3.Connection, roots: Optional[list[Path]]) -> list[dict]:
    rows = []
    for path, sha, verdict, detail in con.execute("SELECT path, sha256, verdict, detail FROM intake"):
        if verdict not in CANDIDATES:
            continue
        if roots and not any(path.lower().startswith(str(r).lower().rstrip("\\/")) for r in roots):
            continue
        if not Path(path).is_file():
            continue
        rows.append({"path": path, "sha256": sha, "verdict": verdict, "detail": json.loads(detail or "{}")})
    return rows


def run(cfg, con: sqlite3.Connection, roots: Optional[list[Path]] = None, sort: bool = True,
        workers: int = 2, retriage: bool = False) -> dict:
    con.executescript(SCHEMA)
    t0 = time.time()
    say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if sort:
        say("ingest: checking the inboxes (exact duplicates deleted, papers moved)")
        s = intake.check(cfg, con, roots=roots, apply=True)
        say(f"  {s['files']} files; {s.get('actions')}")
    # evidence (library matches) comes from the last check's rows: rebuild it from intake details
    evidence = _evidence(cfg)
    rows = _candidates(con, roots)
    for r in rows:
        r["evidence"] = evidence.get(r["path"], {})
    done = {s: json.loads(j) for s, v, j in con.execute("SELECT sha256, version, result FROM ingest_triage")
            if v == TRIAGE_VERSION}
    todo = [r for r in rows if retriage or r["sha256"] not in done]
    say(f"ingest: {len(rows)} books in the inboxes, {len(todo)} to triage with Codex ({workers} at a time)")
    subject_list = _subject_list()
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(triage_one, r, _twin(con, r), subject_list): r for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                res = None
                say(f"  triage crashed for {Path(r['path']).name}: {e}")
            if res is None:
                failed += 1
            else:
                con.execute("INSERT OR REPLACE INTO ingest_triage VALUES (?,?,?,?)",
                            (r["sha256"], TRIAGE_VERSION, json.dumps(res), datetime.now().isoformat(timespec="seconds")))
                con.commit()
                done[r["sha256"]] = res
            say(f"  [{i}/{len(todo)}] {Path(r['path']).name[:70]}: "
                f"{(res or {}).get('category', 'FAILED')} {(res or {}).get('primary', '')}")
    plan = build_plan(cfg, con, rows, done)
    out = render(cfg, plan)
    return {"books": len(rows), "triaged_now": len(todo) - failed, "triage_failed": failed,
            "by_action": _count(plan), "plan": str(plan_path(cfg)), "report": out, "seconds": round(time.time() - t0, 1)}


def _evidence(cfg) -> dict:
    """path -> evidence from the last intake check (reports/intake-check.csv)."""
    import csv
    out = {}
    f = cfg.reports / "intake-check.csv"
    if f.is_file():
        with f.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["library_match"] and row["verdict"] in ("title-match", "isbn-match"):
                    out[row["path"]] = {"library": row["library_match"].split("; ")}
    return out


# ---------------------------------------------------------------------------------------------
# plan

def plan_path(cfg) -> Path:
    return cfg.reports / "ingest-plan.yaml"


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:150]


def filename(title: str, surnames: str, ext: str, edition: Optional[str] = None) -> str:
    """'Title - Surname.pdf', or 'Title - 2nd ed - Surname.pdf' (the library's naming)."""
    t = _safe_name(title) or "Untitled"
    ed = re.sub(r"\s*(edition|edn\.?|ed\.?)\s*$", "", str(edition or ""), flags=re.I).strip()
    if ed and not re.fullmatch(r"1(st)?|first", ed, re.I):
        t += f" - {_safe_name(ed)} ed"
    a = _safe_name(surnames)
    return f"{t} - {a}{ext}" if a else f"{t}{ext}"


def build_plan(cfg, con: sqlite3.Connection, rows: list[dict], triage: dict) -> list[dict]:
    valid = subjects.valid_subjects()
    overrides = {s: json.loads(f) for s, f in con.execute("SELECT sha256, fields FROM ingest_overrides")}
    order = _subject_order()
    items = []
    for r in rows:
        sha, d, t = r["sha256"], r["detail"], triage.get(r["sha256"])
        twin = _twin(con, r)
        it = {"sha256": sha, "path": r["path"], "ext": Path(r["path"]).suffix.lower(),
              "pages": d.get("pages"), "text_layer": d.get("has_text"), "isbn": d.get("isbn") or [],
              "check": r["verdict"]}
        if twin:
            it["library_match"] = twin["path"]
        if not t:
            it.update(action="pending", title=d.get("title_guess"), author=d.get("author_guess"),
                      note="Codex triage failed or not run yet: run ingest again")
        else:
            it.update(title=t.get("title") or d.get("title_guess"), author=t.get("author") or d.get("author_guess"),
                      surnames=t.get("author_surnames") or "", edition=t.get("edition"), year=t.get("year"),
                      category=t.get("category"), primary=t.get("primary"),
                      secondary=[s for s in (t.get("secondary") or []) if s in valid][:3],
                      level=t.get("level"), kind=t.get("kind"), confidence=t.get("confidence"),
                      reason=t.get("reason"))
            if t.get("category") not in ("math", "science", "cs-theory"):
                it["action"] = "hold"
            elif twin and t.get("same_work_as_library_match") is not False:
                if r["verdict"] == "isbn-match" and d.get("has_text") is False and twin.get("has_text"):
                    it["action"], it["note"] = "delete", "same ISBN; a scan, where the library copy has a text layer"
                else:
                    it["action"] = "edition"
            else:
                it["action"] = "library"
            flags = []
            if it["action"] in ("library", "edition") and it["primary"] not in valid:
                flags.append(f"subject '{it['primary']}' is not in the taxonomy: set one")
            if t.get("confidence") == "low":
                flags.append("low confidence")
            if t.get("is_book") is False:
                flags.append("Codex thinks this is not a book")
            if flags:
                it["flags"] = flags
        ov = overrides.get(sha, {})
        it.update(ov)
        if [k for k in ov if k != "flags"]:
            it["overridden"] = sorted(k for k in ov if k != "flags")
        if it.get("action") in ("library", "edition"):
            it["dest"] = str(Path(cfg.sources).joinpath(*str(it.get("primary") or "Unsorted").split("/"))
                             / filename(it.get("title") or "", it.get("surnames") or "", it["ext"], it.get("edition")))
        elif it.get("action") == "hold":
            it["dest"] = str(Path(cfg.hold) / Path(it["path"]).name)
        it["_order"] = (order(it.get("primary")) if it.get("action") == "library" else 9_000,
                        (it.get("primary") or ""), (it.get("title") or "").lower())
        items.append(it)
    rank = {"library": 0, "edition": 1, "hold": 2, "delete": 3, "pending": 4}
    items.sort(key=lambda i: (rank.get(i.get("action"), 5), i["_order"]))
    for n, it in enumerate(items, 1):
        it["n"] = n
        it.pop("_order", None)
    import yaml
    plan_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    plan_path(cfg).write_text("# Generated by `librarian ingest`; adjust with `librarian ingest set N ...`.\n" +
                              yaml.safe_dump({"generated": datetime.now().isoformat(timespec="seconds"), "items": items},
                                             allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    return items


def load_plan(cfg) -> list[dict]:
    import yaml
    f = plan_path(cfg)
    if not f.is_file():
        raise SystemExit("no plan yet: run `librarian ingest` first")
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("items") or []


def _count(plan: list[dict]) -> dict:
    out: dict = {}
    for it in plan:
        out[it.get("action")] = out.get(it.get("action"), 0) + 1
    return out


SUBJECT_ORDER = ["Foundations/Type Theory", "Foundations/Lambda Calculus", "Foundations/Mathematical Logic",
                 "Foundations/Set Theory", "Foundations/Number Systems", "Foundations/", "Analysis/",
                 "Algebra/", "Geometry and Topology/", "Number Theory/", "Probability and Statistics/",
                 "Differential Equations and Dynamics/", "Discrete Mathematics/", "Applied and Computational/",
                 "Computer Science/", "General/", "Calculus/", "Physics/"]


def _subject_order():
    def f(subject: Optional[str]) -> int:
        s = subject or ""
        for i, pre in enumerate(SUBJECT_ORDER):
            if s == pre or (pre.endswith("/") and s.startswith(pre)):
                return i
        return len(SUBJECT_ORDER)
    return f


def _uri(p: str) -> str:
    try:
        return Path(p).absolute().as_uri()
    except ValueError:
        return ""


def render(cfg, plan: list[dict]) -> str:
    c = _count(plan)
    L = ["# Ingest plan", "", f"Generated {datetime.now():%Y-%m-%d %H:%M}. "
         + ", ".join(f"{v} {k}" for k, v in c.items()) + ".", "",
         "Adjust: `scripts\\run.ps1 ingest set N --subject \"Area/Subject\"` (or `--hold`, `--library`, "
         "`--edition`, `--delete`, `--title`, `--author`). Then `scripts\\run.ps1 store --apply`.", ""]
    flagged = [i for i in plan if i.get("flags") or i.get("action") == "pending"]
    if flagged:
        L += ["## Needs a look", ""]
        for it in flagged:
            L.append(f"- **{it['n']}** {it.get('title') or Path(it['path']).name}: "
                     + "; ".join(it.get("flags") or [it.get("note", "")]) + f" — [open]({_uri(it['path'])})")
        L.append("")
    titles = {"library": "Into the library", "edition": "Other editions (into the library, processed last)",
              "hold": "To the hold folder (not maths or science)", "delete": "Delete (worse copies)",
              "pending": "Not triaged"}
    for action in ("library", "edition", "hold", "delete", "pending"):
        sel = [i for i in plan if i.get("action") == action]
        if not sel:
            continue
        L += [f"## {titles[action]} ({len(sel)})", ""]
        cur = None
        for it in sel:
            if action == "library" and it.get("primary") != cur:
                cur = it.get("primary")
                L += ["", f"**{cur}**", ""]
            bits = [f"{it['pages']} pp" if it.get("pages") else None,
                    {True: "text", False: "scan"}.get(it.get("text_layer")),
                    it.get("edition") and f"{it['edition']} ed", str(it["year"]) if it.get("year") else None]
            line = f"{it['n']}. **{it.get('title') or Path(it['path']).name}**"
            if it.get("author"):
                line += f" — {it['author']}"
            line += f" ({', '.join(b for b in bits if b)})" if any(bits) else ""
            line += f" — [open]({_uri(it['path'])})"
            if it.get("library_match"):
                line += f" · library: [{Path(it['library_match']).stem}]({_uri(str(Path(cfg.sources).joinpath(*it['library_match'].split('/'))))})"
            sub = []
            if action in ("library", "edition"):
                sub.append(f"`{it.get('primary')}`" + (f" + {', '.join(it.get('secondary') or [])}" if it.get("secondary") else "")
                           + f" · {it.get('level') or '?'} {it.get('kind') or ''} · {it.get('confidence') or '?'}")
            if it.get("reason"):
                sub.append(it["reason"])
            if it.get("note"):
                sub.append(it["note"])
            if it.get("overridden"):
                sub.append("set by you: " + ", ".join(it["overridden"]))
            L.append(line + ("  \n   " + "  \n   ".join(sub) if sub else ""))
        L.append("")
    out = cfg.reports / "ingest.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return str(out)


def set_item(cfg, con: sqlite3.Connection, n: int, **fields) -> dict:
    con.executescript(SCHEMA)
    plan = load_plan(cfg)
    it = next((i for i in plan if i.get("n") == n), None)
    if not it:
        raise SystemExit(f"no item {n} in the plan")
    fields = {k: v for k, v in fields.items() if v is not None}
    if "primary" in fields and fields["primary"] not in subjects.valid_subjects():
        raise SystemExit(f"'{fields['primary']}' is not in config/taxonomy.yaml")
    row = con.execute("SELECT fields FROM ingest_overrides WHERE sha256=?", (it["sha256"],)).fetchone()
    ov = json.loads(row[0]) if row else {}
    ov.update(fields)
    if fields.get("action") in ("library", "edition") or "primary" in fields:
        ov.pop("flags", None)
        ov["flags"] = []
    con.execute("INSERT OR REPLACE INTO ingest_overrides VALUES (?,?,?)",
                (it["sha256"], json.dumps(ov), datetime.now().isoformat(timespec="seconds")))
    con.commit()
    # rebuild the plan from the stored triage + overrides (numbers can shift if the action changed)
    rows = [{"path": i["path"], "sha256": i["sha256"], "verdict": i["check"],
             "detail": {"pages": i.get("pages"), "has_text": i.get("text_layer"), "isbn": i.get("isbn"),
                        "title_guess": i.get("title"), "author_guess": i.get("author")},
             "evidence": {"library": [i["library_match"]]} if i.get("library_match") else {}} for i in plan]
    triage = {s: json.loads(j) for s, j in con.execute("SELECT sha256, result FROM ingest_triage")}
    new = build_plan(cfg, con, rows, triage)
    render(cfg, new)
    moved = next(i for i in new if i["sha256"] == it["sha256"])
    return {"item": moved["n"], "action": moved.get("action"), "primary": moved.get("primary"),
            "dest": moved.get("dest"), "report": str(cfg.reports / "ingest.md")}


# ---------------------------------------------------------------------------------------------
# store

def _free(dest: Path) -> Path:
    n = 2
    out = dest
    while out.exists():
        out = dest.with_name(f"{dest.stem} - {n}{dest.suffix}")
        n += 1
    return out


def _copy_verified(src: Path, dest: Path, sha: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _free(dest)
    shutil.copy2(src, dest)
    if sha256_file(dest) != sha:
        dest.unlink()
        raise OSError(f"copy to {dest} did not verify")
    intake._unlink(src)
    return dest


def batch_running(cfg) -> bool:
    f = cfg.reports / "ocr-batch-status.md"
    if not f.is_file() or time.time() - f.stat().st_mtime > 180:
        return False
    text = f.read_text(encoding="utf-8", errors="replace")
    return not re.search(r"State: \*\*(stopped|finished)", text)


def store(cfg, con: sqlite3.Connection, apply: bool = False, start_ocr: bool = True) -> dict:
    plan = load_plan(cfg)
    con.executescript(intake.SCHEMA)
    todo = [i for i in plan if i.get("action") in ("library", "edition", "hold", "delete") and Path(i["path"]).is_file()]
    blocked = [i for i in todo if i.get("action") in ("library", "edition")
               and i.get("primary") not in subjects.valid_subjects()]
    if blocked:
        raise SystemExit("set a subject first for item(s) " + ", ".join(str(i["n"]) for i in blocked)
                         + " (librarian ingest set N --subject Area/Subject)")
    pending = [i for i in plan if i.get("action") == "pending"]
    if not apply:
        return {"dry_run": True, "would": _count(todo), "not_triaged": len(pending),
                "note": "run `librarian store --apply` to do it"}
    now = datetime.now().isoformat(timespec="seconds")
    log = (cfg.reports / "intake-actions.tsv").open("a", encoding="utf-8")
    filed, held, deleted, errors = [], [], [], []
    tags = subjects.load(subjects.SUBJECTS_FILE)
    entries = {k: dict(v) for k, v in tags.items()}
    for it in todo:
        src = Path(it["path"])
        try:
            if it["action"] == "delete":
                intake._unlink(src)
                log.write(f"{now}\tdelete\tworse-copy\t{it['sha256']}\t{src}\t{it.get('library_match')}\n")
                deleted.append(it)
            elif it["action"] == "hold":
                dest = _copy_verified(src, Path(it["dest"]), it["sha256"])
                con.execute("INSERT OR REPLACE INTO intake_done VALUES (?,?,?,?)", (it["sha256"], "hold", str(dest), now))
                log.write(f"{now}\tmove\thold\t{it['sha256']}\t{src}\t{dest}\n")
                held.append(it)
            else:
                dest = _copy_verified(src, Path(it["dest"]), it["sha256"])
                it["dest"] = str(dest)
                con.execute("INSERT OR REPLACE INTO intake_done VALUES (?,?,?,?)", (it["sha256"], "filed", str(dest), now))
                log.write(f"{now}\tmove\t{it['action']}\t{it['sha256']}\t{src}\t{dest}\n")
                entries[it["sha256"][:16]] = {k: v for k, v in {
                    "title": it.get("title"), "primary": it.get("primary"), "secondary": it.get("secondary") or [],
                    "level": it.get("level"), "kind": it.get("kind"), "confidence": it.get("confidence"),
                    "note": it.get("reason")}.items() if v is not None}
                filed.append(it)
            con.execute("DELETE FROM intake WHERE path=?", (str(src),))
            con.commit()
        except OSError as e:
            errors.append(f"{it['n']}: {e}")
            log.write(f"{now}\terror\t{it['action']}\t{it['sha256']}\t{src}\t{e}\n")
    log.close()
    if filed:
        subjects.write(entries, subjects.SUBJECTS_FILE)
    rel = lambda it: Path(it["dest"]).relative_to(Path(cfg.sources)).as_posix()  # noqa: E731
    editions = [i for i in filed if i["action"] == "edition"]
    if editions:
        with (REPO / "config" / "lower-priority.txt").open("a", encoding="utf-8") as f:
            for it in editions:
                f.write(rel(it) + "\n")
    out = {"filed": len(filed), "editions": len(editions), "held": len(held), "deleted": len(deleted),
           "errors": errors, "not_triaged": len(pending)}
    if not filed:
        return out
    # index the new files (inventory adds them as books), then queue them in the library's order
    r = subprocess.run([sys.executable, "-m", "librarian", "inventory"], cwd=REPO, capture_output=True, text=True)
    out["inventory_exit"] = r.returncode
    queue = write_queue(cfg, filed)
    out["queue"] = str(queue)
    add_to_leancheck(filed, rel)
    out["ocr"] = queue_ocr(cfg, queue) if start_ocr else "not started (--no-ocr)"
    return out


def write_queue(cfg, filed: list[dict]) -> Path:
    order = _subject_order()

    def key(it):
        repair = it.get("pages") or 0 if it.get("text_layer") is False else 0   # scans need every page
        return (it["action"] == "edition", order(it.get("primary")), repair, it.get("pages") or 0)
    q = REPO / "config" / "ocr-queue-intake.txt"
    lines = [f"# Books filed by `librarian store` on {datetime.now():%Y-%m-%d %H:%M}, in processing order:",
             "# Foundations (type theory, logic, sets, number systems) first, then Analysis and the other areas,",
             "# Calculus and Physics late, other editions last. Run (store starts this itself):",
             "#   .venv\\Scripts\\python.exe scripts\\ocr_batch.py --queue config\\ocr-queue-intake.txt --all-pages --no-audit", ""]
    for it in sorted(filed, key=key):
        scan = "scan" if it.get("text_layer") is False else "text"
        lines.append(f"{it['sha256'][:16]}  #  {it.get('pages') or '?':>4} pages  {scan}  "
                     f"{'edition  ' if it['action'] == 'edition' else ''}{it.get('primary')}  {Path(it['dest']).name}")
    if q.is_file():   # keep books from an earlier store that are still waiting
        old = [ln for ln in q.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
        new_keys = {ln[:16] for ln in lines if ln and not ln.startswith("#")}
        lines += [ln for ln in old if ln[:16] not in new_keys]
    q.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return q


def add_to_leancheck(filed: list[dict], rel) -> None:
    f = REPO / "config" / "leancheck-books.txt"
    if not f.is_file():
        return
    text = f.read_text(encoding="utf-8")
    new = [it for it in filed if it["action"] == "library"]
    eds = [it for it in filed if it["action"] == "edition"]
    marker = "## Weaker copies"
    sec = ""
    if new:
        sec = f"## Newly ingested ({datetime.now():%Y-%m-%d}; placed properly when the list is regenerated)\n" + \
              "".join(f"{rel(it)}  # new, {it.get('pages') or '?'} pp\n" for it in new) + "\n"
    if marker in text:
        i = text.index(marker)
        text = text[:i] + sec + text[i:]
    else:
        text = text.rstrip("\n") + "\n\n" + sec
    text = text.rstrip("\n") + "\n" + "".join(f"{rel(it)}  # other edition, {it.get('pages') or '?'} pp\n" for it in eds)
    f.write_text(text, encoding="utf-8")


def queue_ocr(cfg, queue: Path) -> str:
    """Hand the queue to a running OCR batch (it does these books next), or start one."""
    if batch_running(cfg):
        (cfg.reports / "ocr-batch.insert").write_text(json.dumps({"queue": str(queue), "all_pages": True}),
                                                      encoding="utf-8")
        return "handed to the running OCR batch: it does these books next"
    cmd = [sys.executable, str(REPO / "scripts" / "ocr_batch.py"), "--queue", str(queue), "--all-pages", "--no-audit"]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, cwd=REPO, creationflags=flags)
    return "started an OCR batch in a new window"
