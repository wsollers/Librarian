import hashlib
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

from librarian import intake


def test_isbn13_validates_and_converts():
    assert intake.isbn13("0-387-90093-4") == "9780387900933"          # ISBN-10 -> 13
    assert intake.isbn13("978-0-387-90093-3") == "9780387900933"
    assert intake.isbn13("978-0-387-90093-4") is None                 # bad check digit
    assert intake.isbn13("0-387-90093-5") is None


def test_identifiers_from_a_copyright_page():
    ids = intake.identifiers("Second Edition\nISBN 978-3-031-55964-8\n© Springer 2024\n"
                             "https://doi.org/10.1007/978-3-031-55964-8\n1st edition: © 2009")
    assert ids["isbn"] == ["9783031559648"] and ids["edition"] == "Second Edition"
    assert ids["doi"] == ["10.1007/978-3-031-55964-8"] and ids["year"] == 2024


def _cfg(tmp_path, inbox):
    data = tmp_path / "data"
    return SimpleNamespace(inbox=[inbox], inbox_exclude=[inbox / "skip"], sources=tmp_path / "Sources",
                           data=data, publish=None, objects=data / "objects", reports=data / "reports",
                           papers=tmp_path / "Papers", hold=tmp_path / "Hold")


def _epub(path: Path, title: str, isbn: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("OEBPS/content.opf", f'<package><metadata><dc:title>{title}</dc:title>'
                   f'<dc:creator>A. Author</dc:creator><dc:identifier>urn:isbn:{isbn}</dc:identifier>'
                   f'</metadata></package>')
        z.writestr("filler.bin", b"\0" * 200_000)


def test_check_sorts_the_inbox(tmp_path, monkeypatch):
    inbox = tmp_path / "Downloads"
    (inbox / "skip").mkdir(parents=True)
    lib_bytes = b"%PDF library copy" + b"x" * 200_000
    (inbox / "Same - Author.pdf").write_bytes(lib_bytes)                        # = library file
    (inbox / "paper.pdf").write_bytes(b"%PDF paper" + b"p" * 200_000)
    (inbox / "paper copy.pdf").write_bytes(b"%PDF paper" + b"p" * 200_000)       # = another inbox file
    (inbox / "tiny.pdf").write_bytes(b"%PDF" * 10)                               # under 100 KB
    (inbox / "book.pdf.crdownload").write_bytes(b"x" * 200_000)                  # partial download
    (inbox / "skip" / "x.pdf").write_bytes(b"%PDF skip" + b"s" * 200_000)        # excluded folder
    _epub(inbox / "Measure Theory - Halmos.epub", "Measure Theory", "0-387-90088-8")
    _epub(inbox / "Fresh Book.epub", "A Completely Fresh Book", "978-3-031-55964-8")
    real_probe = intake.probe   # EPUBs are probed for real; the fake PDFs get a stub (a 12-page paper)
    monkeypatch.setattr(intake, "probe", lambda p: real_probe(p) if p.suffix == ".epub" else
                        {"pages": 12, "has_text": True, "title_guess": "Paper", "isbn": [], "arxiv": None})

    cfg = _cfg(tmp_path, inbox)
    con = sqlite3.connect(tmp_path / "lib.sqlite")
    con.execute("CREATE TABLE books (sha256 TEXT, path TEXT, title TEXT, author TEXT)")
    lib_sha = hashlib.sha256(lib_bytes).hexdigest()
    halmos = "h" * 64
    con.executemany("INSERT INTO books VALUES (?,?,?,?)", [
        (lib_sha, "Algebra/Linear Algebra/Same - Author.pdf", "Same", "Author"),
        (halmos, "Analysis/Measure and Integration/Measure Theory - Halmos.pdf", "Measure Theory", "Halmos")])
    pages = cfg.objects / halmos / "pages"
    pages.mkdir(parents=True)
    (pages / "0004.md").write_text("ISBN 0-387-90088-8\n", encoding="utf-8")

    s = intake.check(cfg, con, verbose=False)
    by = {Path(r[0]).name: r[1] for r in con.execute("SELECT path, verdict FROM intake")}
    assert by["Same - Author.pdf"] == "duplicate"
    assert {by["paper.pdf"], by["paper copy.pdf"]} == {"paper", "duplicate-in-inbox"}
    assert by["Measure Theory - Halmos.epub"] == "isbn-match"
    assert by["Fresh Book.epub"] == "new"
    assert "tiny.pdf" not in by and "x.pdf" not in by and s["skipped"]["partial"] == 1
    assert (cfg.reports / "intake-check.md").is_file() and s["by_verdict"]["new"] == 1

    # check is read-only; sort --apply deletes exact duplicates and moves papers (copy, verify, delete)
    assert (inbox / "Same - Author.pdf").exists() and (inbox / "paper.pdf").exists()
    s = intake.check(cfg, con, apply=True, verbose=False)
    assert s["actions"] == {"deleted": 2, "papers_moved": 1, "errors": 0}
    assert not (inbox / "Same - Author.pdf").exists()
    papers = list((tmp_path / "Papers").iterdir())
    assert len(papers) == 1 and papers[0].read_bytes() == b"%PDF paper" + b"p" * 200_000
    assert (inbox / "Fresh Book.epub").exists() and (inbox / "Measure Theory - Halmos.epub").exists()
    assert len((cfg.reports / "intake-actions.tsv").read_text(encoding="utf-8").splitlines()) == 3

    # the same paper turning up again is a duplicate of the filed copy
    (inbox / "paper again.pdf").write_bytes(b"%PDF paper" + b"p" * 200_000)
    intake.check(cfg, con, verbose=False)
    by = {Path(r[0]).name: r[1] for r in con.execute("SELECT path, verdict FROM intake")}
    assert by["paper again.pdf"] == "duplicate"
