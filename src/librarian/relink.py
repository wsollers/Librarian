"""Keep the path-pattern lists in config/*.txt pointing at books after files move.

`--select @file` matches each line as a case-insensitive substring of the path under the sources
root, so a pattern with a folder in it ("Analysis/Real Analysis/Elementary Real Analysis - Bruckner")
stops matching once scripts/move-to-subjects.ps1 files the book elsewhere. Given that script's
move log, rewrite every such pattern to the new folder; file-name-only patterns already still match.
Patterns naming only a folder, or several moved books going to different folders, are reported
and left alone.

  python -m librarian.relink <move-log.csv> [--reverse] [--dir config]
Prints to stdout only (PowerShell 5.1 stops a script on native stderr).
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def new_pattern(pat: str, moves: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """(rewritten pattern or None if unchanged, problem or None)."""
    low = pat.lower()
    if "/" not in pat:
        return None, None
    targets = set()
    for f, t in moves:
        pos = f.lower().find(low)
        if pos < 0:
            continue
        name_start = f.rfind("/") + 1
        slash = pos + low.rfind("/")
        if slash != name_start - 1:          # the pattern's last "/" is not the folder/name boundary
            return None, f"folder-only pattern matches moved file {f}"
        targets.add(t[:t.rfind("/") + 1] + pat[pat.rfind("/") + 1:])
    if not targets:
        return None, None
    if len(targets) > 1:
        return None, f"matches moved files that went to different folders: {sorted(targets)[:3]}"
    new = targets.pop()
    return (new if new != pat else None), None


def relink_file(path: Path, moves: list[tuple[str, str]]) -> tuple[int, list[str]]:
    raw = path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    out, changed, problems = [], 0, []
    for line in raw.split(nl):
        body, hash_, comment = line.partition("#")
        pat = body.strip()
        if pat:
            new, problem = new_pattern(pat, moves)
            if problem:
                problems.append(f"{path.name}: {pat!r}: {problem}")
            if new:
                line = body.replace(pat, new, 1) + hash_ + comment
                changed += 1
        out.append(line)
    if changed:
        path.write_bytes(nl.join(out).encode("utf-8"))
    return changed, problems


def load_moves(log: Path, reverse: bool = False) -> list[tuple[str, str]]:
    with log.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("result") == "moved"]
    pairs = [(r["from"].replace("\\", "/"), r["to"].replace("\\", "/")) for r in rows]
    return [(t, f) for f, t in pairs] if reverse else pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="librarian.relink")
    ap.add_argument("log", type=Path)
    ap.add_argument("--reverse", action="store_true", help="the log was undone: map new paths back to old")
    ap.add_argument("--dir", type=Path, default=Path(__file__).resolve().parents[2] / "config")
    a = ap.parse_args(argv)
    moves = load_moves(a.log, a.reverse)
    total, problems = 0, []
    for f in sorted(a.dir.glob("*.txt")):
        n, p = relink_file(f, moves)
        total += n
        problems += p
        if n:
            print(f"  {f.name}: {n} patterns updated")
    print(f"relink: {total} patterns updated in {a.dir} for {len(moves)} moved files")
    for p in problems:
        print(f"  check by hand: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
