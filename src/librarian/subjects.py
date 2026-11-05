"""Subject tags: config/subjects.yaml gives a book one primary subject and up to three secondary ones,
each "Area/Subject" from config/taxonomy.yaml.

The subject filter uses a book's tags when it has them and its folder otherwise, so a book filed under
the wrong folder is still found under the subject it is really about. Tags were first proposed from
each book's profile (table of contents + preface); edit the file to correct them.

The file is keyed by the first 16 hex digits of the book's sha256:

    8ede9f42eb5436c8:
      title: Real Analysis (Folland)
      primary: Analysis/Measure and Integration
      secondary: [Analysis/Functional Analysis, Analysis/Fourier Analysis]
      level: graduate
      kind: textbook
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
SUBJECTS_FILE = REPO / "config" / "subjects.yaml"
TAXONOMY_FILE = REPO / "config" / "taxonomy.yaml"

_cache: dict = {"mtime": None, "path": None, "tags": {}}


def taxonomy(path: Path = TAXONOMY_FILE) -> dict[str, list[str]]:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def valid_subjects(path: Path = TAXONOMY_FILE) -> set[str]:
    return {f"{area}/{s}" for area, subs in taxonomy(path).items() for s in (subs or [])}


def load(path: Path = SUBJECTS_FILE) -> dict[str, dict]:
    """sha16 -> entry; re-read when the file changes, {} when there is none."""
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    if _cache["path"] != path or _cache["mtime"] != mtime:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _cache.update(path=path, mtime=mtime, tags={str(k).lower(): v for k, v in data.items() if isinstance(v, dict)})
    return _cache["tags"]


def tags_of(entry: Optional[dict]) -> list[str]:
    """The book's subjects, primary first."""
    if not entry:
        return []
    out = [entry.get("primary")] + list(entry.get("secondary") or [])
    return [t for t in out if t]


def book_tags(sha: str, tags: Optional[dict] = None) -> list[str]:
    tags = load() if tags is None else tags
    return tags_of(tags.get(sha[:16].lower()))


def write(entries: dict[str, dict], path: Path = SUBJECTS_FILE) -> None:
    """Write subjects.yaml: entries sorted by primary subject, then title, so the file reads by subject."""
    import yaml
    head = ("# Subject tags per book (sha256 prefix -> primary + secondary subjects from config/taxonomy.yaml).\n"
            "# Proposed from each book's table of contents and preface; edit to correct. The subject filter\n"
            "# uses these instead of the folder; scripts/move-to-subjects.ps1 files PDFs by primary subject.\n")
    order = sorted(entries.items(), key=lambda kv: (kv[1].get("primary") or "", (kv[1].get("title") or "").lower()))
    body = yaml.safe_dump({k: v for k, v in order}, allow_unicode=True, sort_keys=False, width=110)
    path.write_text(head + body, encoding="utf-8")


def _safe(name: str) -> str:
    """A folder name Windows accepts."""
    return re.sub(r'[<>:"|?*\\]', "-", name).strip(" .")


def move_plan(con: sqlite3.Connection, entries: Optional[dict] = None) -> list[dict]:
    """Where each tagged book's files go: <Area>/<Subject>/<same file name> under the sources root,
    from its primary subject. Only files not already there; every file of a book (duplicate copies
    too) moves. Rows whose target name is taken (by another file, or twice in this plan) are kept
    with status 'conflict' and are not moved."""
    entries = load() if entries is None else entries
    rows, taken = [], set()
    files = con.execute("SELECT f.path, f.sha256 FROM files f JOIN books b USING (sha256) ORDER BY f.path").fetchall()
    existing = {p.lower() for p, _ in files}
    for path, sha in files:
        e = entries.get(sha[:16].lower())
        if not e or not e.get("primary") or "/" not in e["primary"]:
            continue
        area, subject = e["primary"].split("/", 1)
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        target = f"{_safe(area)}/{_safe(subject)}/{name}"
        if target.lower() == path.replace("\\", "/").lower():
            continue
        status = "move"
        if target.lower() in existing or target.lower() in taken:
            status = "conflict"
        taken.add(target.lower())
        rows.append({"status": status, "from": path.replace("\\", "/"), "to": target, "sha": sha[:16],
                     "subject": e["primary"]})
    return rows


def write_plan(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["status", "from", "to", "sha", "subject"])
        w.writeheader()
        w.writerows(rows)


def review_csv(entries: dict[str, dict], folders: dict[str, str], out: Path) -> int:
    """One row per book: current folder against proposed subjects, books leaving their folder first."""
    rows = []
    for sha, e in entries.items():
        folder = folders.get(sha, "")
        primary = e.get("primary") or ""
        top = folder.split("/")[0].lower()
        moved = not (top and (top in primary.lower() or folder.split("/")[-1].lower() in primary.lower()))
        rows.append({"moves": "yes" if moved else "", "title": e.get("title", ""), "current_folder": folder,
                     "primary": primary, "secondary": "; ".join(e.get("secondary") or []),
                     "level": e.get("level", ""), "kind": e.get("kind", ""), "confidence": e.get("confidence", ""),
                     "note": e.get("note", ""), "new_subject": e.get("new_subject") or "", "sha": sha})
    rows.sort(key=lambda r: (r["moves"] != "yes", r["primary"], r["title"].lower()))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:     # BOM: Excel opens it as UTF-8
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["sha"])
        w.writeheader()
        w.writerows(rows)
    return sum(1 for r in rows if r["moves"])
