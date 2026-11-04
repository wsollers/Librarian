import sqlite3
from pathlib import Path

from librarian import config, inventory


def make_cfg(tmp_path: Path):
    src = tmp_path / "Sources" / "Logic"
    src.mkdir(parents=True)
    (src / "A Book - Someone.zip").write_bytes(b"same bytes")
    (src / "Copy - Someone.zip").write_bytes(b"same bytes")
    (src / "Other - Else.png").write_bytes(b"different")
    return config.load(
        None, sources=tmp_path / "Sources", data=tmp_path / "data",
        legacy=tmp_path / "nolegacy", manifest=tmp_path / "none.yaml",
    )


def test_inventory_dedupes_and_reports(tmp_path):
    cfg = make_cfg(tmp_path)
    s = inventory.run(cfg, verbose=False)
    assert s["files"] == 3 and s["books"] == 2 and s["duplicates"] == 1
    assert s["status"] == {"unsupported": 2}
    report = (cfg.reports / "coverage.md").read_text()
    assert "## unsupported (2)" in report and "## duplicates (1)" in report


def test_inventory_is_incremental(tmp_path):
    cfg = make_cfg(tmp_path)
    inventory.run(cfg, verbose=False)
    assert inventory.run(cfg, verbose=False)["hashed"] == 0
    (cfg.sources / "Logic" / "Other - Else.png").unlink()
    s = inventory.run(cfg, verbose=False)
    assert s["removed"] == 1 and s["books"] == 1
    con = sqlite3.connect(cfg.db_path)
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 1


def test_title_from_filename():
    t = inventory.title_from_filename
    assert t("Logic/A Mathematical Introduction to Logic - Enderton - 2.pdf") == ("A Mathematical Introduction to Logic", "Enderton")
    assert t("Logic/Introduction to Mathematical Logic - 6th - Mendelson.pdf") == ("Introduction to Mathematical Logic (6th)", "Mendelson")
    assert t("Analysis/cantor.png") == ("cantor", None)


def test_inventory_purges_vanished_book_with_pages(tmp_path):
    cfg = make_cfg(tmp_path)
    inventory.run(cfg, verbose=False)
    con = sqlite3.connect(cfg.db_path)
    con.execute("PRAGMA foreign_keys=ON")
    sha = con.execute("SELECT sha256 FROM files WHERE path LIKE '%Other%'").fetchone()[0]
    con.execute("INSERT INTO pages(sha256, pdf_page) VALUES(?, 1)", (sha,))  # extracted book
    con.commit()
    con.close()
    (cfg.sources / "Logic" / "Other - Else.png").unlink()
    s = inventory.run(cfg, verbose=False)  # used to fail: FOREIGN KEY constraint failed
    assert s["books"] == 1
    con = sqlite3.connect(cfg.db_path)
    assert con.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
