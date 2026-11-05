"""Page lines read from the index must equal the page files' lines, which citations count against."""
import sqlite3

from librarian.config import Config
from librarian.pagemap import read_pages
from librarian.search import PageText, _page_lines

SHA = "b" * 64
PAGES = {1: "Definition 1. A set is compact if\nevery open cover has a finite subcover.\n",
         2: "\n\nTheorem 2 (Heine-Borel).\n  Closed and bounded.\n\n",
         3: "single line\n",
         4: "Ünïcödé — ligatures ﬁ\nand tabs\there\n"}


def _setup(tmp_path):
    cfg = Config()
    cfg.data = tmp_path
    pages_dir = cfg.objects / SHA / "pages"
    pages_dir.mkdir(parents=True)
    for p, text in PAGES.items():
        (pages_dir / f"{p:04d}.md").write_text(text, encoding="utf-8", newline="\n")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE pages_fts USING fts5(text, sha256 UNINDEXED, pdf_page UNINDEXED)")
    # Exactly what index.py stores.
    con.executemany("INSERT INTO pages_fts(text, sha256, pdf_page) VALUES(?,?,?)",
                    [("\n".join(lines), SHA, p) for p, lines in read_pages(cfg.objects / SHA).items() if lines])
    return cfg, con


def test_index_lines_equal_file_lines(tmp_path):
    cfg, con = _setup(tmp_path)
    pt = PageText(con)
    for p in PAGES:
        assert pt.lines(cfg, SHA, p) == _page_lines(cfg, SHA, p)


def test_falls_back_to_the_file(tmp_path):
    cfg, con = _setup(tmp_path)
    pt = PageText(con)
    # Not in the index at all.
    (cfg.objects / SHA / "pages" / "0009.md").write_text("only on disk\n", encoding="utf-8", newline="\n")
    assert pt.lines(cfg, SHA, 9) == ["only on disk", ""]
    # The index was rebuilt after the map was made: the remembered row now holds another page.
    con.execute("DELETE FROM pages_fts")
    con.execute("INSERT INTO pages_fts(rowid, text, sha256, pdf_page) VALUES(1, 'other book', 'c', 7)")
    assert pt.lines(cfg, SHA, 1) == _page_lines(cfg, SHA, 1)
    assert pt.lines(cfg, "nope", 1) == []
