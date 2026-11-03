"""Side-by-side OCR test: run nougat on a book into objects/<sha>/nougat/ and compare each page
with the current page files (e.g. Codex OCR). Nothing in pages/ is changed."""

from __future__ import annotations

import json
import re
import statistics
from difflib import SequenceMatcher
from pathlib import Path

from . import ocr
from .config import Config
from .log import Timer, log
from .text import clean_lines, quality

_CMD = re.compile(r"\\[A-Za-z]+")
_WORD = re.compile(r"[A-Za-z0-9]+")


def words(text: str) -> list[str]:
    """Comparable tokens: LaTeX macro names dropped, so \\mathbb{R} vs ℝ-style differences matter less."""
    return _WORD.findall(_CMD.sub(" ", text).lower())


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa and not wb:
        return 1.0
    return round(SequenceMatcher(None, wa, wb, autojunk=False).ratio(), 3)


def nougat_test(cfg: Config, sha: str, title: str, pages: int) -> dict:
    obj = cfg.objects / sha
    src_rel = json.loads((obj / "extract.json").read_text(encoding="utf-8"))["path"]
    pdf = cfg.sources / src_rel
    out = obj / "nougat"
    out.mkdir(parents=True, exist_ok=True)
    t = Timer()
    log.info("nougat-test: %s (%d pages)", title, pages)
    got = ocr.nougat_ocr(pdf, list(range(1, pages + 1)))
    secs = t.s
    rows = []
    for p in range(1, pages + 1):
        md, state = got.get(p, ("", "missing"))
        lines = clean_lines(md)
        (out / f"{p:04d}.md").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
        cur = (obj / "pages" / f"{p:04d}.md")
        cur_text = cur.read_text(encoding="utf-8") if cur.is_file() else ""
        rows.append({"page": p, "state": state, "quality": quality(lines),
                     "similarity": similarity(cur_text, "\n".join(lines)),
                     "nougat_words": len(words(md)), "current_words": len(words(cur_text))})
    ok = [r for r in rows if r["state"] == "ok"]
    sims = [r["similarity"] for r in ok if r["current_words"] > 30]
    report = {
        "sha256": sha, "title": title, "pages": pages, "seconds": round(secs, 1),
        "pages_per_min": round(pages / max(secs, 1e-6) * 60, 1),
        "states": {s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}},
        "similarity_median": statistics.median(sims) if sims else None,
        "similarity_below_0.7": [r["page"] for r in ok if r["current_words"] > 30 and r["similarity"] < 0.7],
        "rows": rows,
    }
    (out / "compare.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    log.info("  %.1f pages/min; states %s; median similarity to current pages %s; %d pages below 0.7",
             report["pages_per_min"], report["states"], report["similarity_median"],
             len(report["similarity_below_0.7"]))
    log.info("  per-page results: %s", out / "compare.json")
    return {k: v for k, v in report.items() if k != "rows"}
