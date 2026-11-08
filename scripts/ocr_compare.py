"""Compare OCR models on a few pages: re-OCR with another Codex model and set the result beside the
page text already in the library.

  .venv\\Scripts\\python.exe scripts\\ocr_compare.py 5ca845be90e7dbcc 76 98 --model gpt-6-luna

Writes <data>\\reports\\ocr-compare\\: the page images, each model's text, and compare.md
(similarity, timing, both texts side by side). Nothing in the library is changed.
"""
import argparse
import difflib
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)
from librarian import config, ocr  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("book", help="sha256 prefix")
ap.add_argument("pages", nargs="+", type=int, help="PDF page numbers")
ap.add_argument("--model", default="gpt-6-luna")
a = ap.parse_args()

cfg = config.load(None)
con = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
sha, path, title = con.execute("SELECT sha256, path, title FROM books WHERE sha256 LIKE ?", (a.book + "%",)).fetchone()
pdf = cfg.sources / path
out = cfg.reports / "ocr-compare"
out.mkdir(parents=True, exist_ok=True)
os.environ["LIBRARIAN_CODEX_MODEL"] = a.model
L = [f"# OCR comparison: {title}", "", f"Library text (current model) vs `{a.model}`. Page images are beside this file.", ""]
for p in a.pages:
    old_f = cfg.objects / sha / "pages" / f"{p:04d}.md"
    old = old_f.read_text(encoding="utf-8").strip() if old_f.is_file() else ""
    img = ocr.render_page(pdf, p, out)
    shutil.move(str(img), out / f"{sha[:16]}-p{p:04d}.png")
    t = time.time()
    new = (ocr.codex_ocr(pdf, p) or "").strip()
    secs = time.time() - t
    (out / f"{sha[:16]}-p{p:04d}-{a.model}.md").write_text(new + "\n", encoding="utf-8")
    (out / f"{sha[:16]}-p{p:04d}-library.md").write_text(old + "\n", encoding="utf-8")
    ratio = difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()
    ol, nl = old.splitlines(), new.splitlines()
    diff = list(difflib.unified_diff(ol, nl, "library", a.model, lineterm="", n=0))
    L += [f"## PDF page {p}", "",
          f"- similarity to the library text: **{ratio:.3f}**; lines {len(ol)} vs {len(nl)}; "
          f"`$` count {old.count('$')} vs {new.count('$')}; {a.model} took {secs:.0f} s",
          f"- image: `{sha[:16]}-p{p:04d}.png`", "",
          "### Differences (library → " + a.model + ")", "", "```diff", *diff[:200], "```", ""]
    print(f"p{p}: similarity {ratio:.3f}, {secs:.0f}s, {len(diff)} diff lines")
(out / "compare.md").write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"report: {out / 'compare.md'}")
