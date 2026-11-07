"""Remove Codex's remarks ("I'll inspect the page at full resolution ...") from OCR'd pages.

Finds affected pages through the full-text index, rewrites the page files and the OCR cache
without them, and writes config/fix-narration.txt (the books to re-index):
    python scripts/fix_narration.py [data dir]
    scripts\\run.ps1 index --select @config/fix-narration.txt --force
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from librarian.ocr import _NARRATION, strip_narration  # noqa: E402

data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"F:\Readings\librarian")
con = sqlite3.connect((data / "librarian.sqlite").resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
hits = [(sha, p) for t, sha, p in con.execute("SELECT c0, c1, c2 FROM pages_fts_content WHERE c0 LIKE 'I%' OR c0 LIKE 'Let me%'")
        if _NARRATION.match(t or "")]
books = sorted({sha for sha, _ in hits})
fixed = 0
for sha, p in hits:
    for f in (data / "objects" / sha / "pages" / f"{p:04d}.md", data / "objects" / sha / "ocr-cache" / f"{p:04d}.md"):
        if f.is_file():
            old = f.read_text(encoding="utf-8")
            new = strip_narration(old)
            if new != old.strip("\n"):
                f.write_text(new + ("\n" if new else ""), encoding="utf-8", newline="\n")
                fixed += 1
(REPO / "config" / "fix-narration.txt").write_text(
    "# books whose pages had Codex remarks removed: re-index with --force\n" + "".join(s[:16] + "\n" for s in books),
    encoding="utf-8")
print(f"{len(hits)} pages in {len(books)} books; {fixed} files rewritten; list: config/fix-narration.txt")
