"""The subject filter selects books by their subject folders."""
import sqlite3

from librarian import search

BOOKS = {
    "r1": "Analysis/Real Analysis/Rudin.pdf",
    "r2": "Analysis/Real Analysis/Tao.pdf",
    "m1": "Analysis/Metric Spaces/Searcoid.pdf",
    "c1": "Analysis/Complex Analysis/Ahlfors.pdf",
    "a0": "Analysis/Apostol.pdf",
    "n1": "Numerical Analysis/Trefethen.pdf",
    "t1": "Topology/Munkres.pdf",
    "p1": "Differential Equations/Partial Differential Equations/Taylor.pdf",
    "g1": "Analysis/Differential Geometry/Lee.pdf",
    "x1": "Topology/Not Indexed.pdf",
}


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE books(sha256 TEXT PRIMARY KEY, path TEXT, title TEXT, author TEXT);
    CREATE TABLE index_state(sha256 TEXT PRIMARY KEY);
    CREATE TABLE sections(sha256 TEXT, seq INT, level INT, number TEXT, title TEXT, printed_page TEXT,
                          pdf_start INT, pdf_end INT, source TEXT, verified INT);
    """)
    con.executemany("INSERT INTO books VALUES(?,?,?,?)", [(s, p, p, "") for s, p in BOOKS.items()])
    con.executemany("INSERT INTO index_state VALUES(?)", [(s,) for s in BOOKS if s != "x1"])
    con.execute("INSERT INTO sections VALUES('r1', 1, 1, '2', 'Basic Topology', '24', 30, 60, 'toc', 1)")
    return con


def shas(subject):
    return search.subject_books(_db(), subject)[0]


def test_a_name_includes_its_subfolders_but_not_other_fields_with_that_word():
    assert shas("Analysis") == {"r1", "r2", "m1", "c1", "a0", "g1"}          # not Numerical Analysis
    assert shas("real analysis") == {"r1", "r2"}
    assert shas("Metric Spaces") == {"m1"}


def test_several_subjects_and_paths():
    assert shas("Real Analysis, Metric Spaces; Topology") == {"r1", "r2", "m1", "t1"}
    assert shas("Analysis/Real Analysis") == {"r1", "r2"}
    assert shas("Numerical Analysis") == {"n1"}


def test_partial_names_and_unknown_ones():
    # Not a folder's exact name: folders containing it.
    assert shas("Differential") == {"p1", "g1"}
    got, matched, unknown = search.subject_books(_db(), "Real Analysis, Topolgy")
    assert got == {"r1", "r2"} and matched == ["Analysis/Real Analysis"] and unknown == ["topolgy"]
    assert search.subject_books(_db(), "Astrology") == (set(), [], ["astrology"])


def test_unindexed_books_are_left_out():
    assert shas("Topology") == {"t1"}


def test_scope_combines_subject_with_other_filters():
    con = _db()
    real = search.subject_books(con, "Real Analysis")[0]
    assert search.scope(con, subject_shas=real) == {"r1": None, "r2": None}
    assert search.scope(con, chapter="Basic Topology", subject_shas=real) == {"r1": [(30, 60)]}
    assert search.scope(con, book="Tao", subject_shas=real) == {"r2": None}
    assert search.scope(con, book="Munkres", subject_shas=real) == {}
    assert search.scope(con, subject_shas=set()) == {}


def test_subject_list_counts_subfolders():
    got = {s["subject"]: s["books"] for s in search.subjects(_db())}
    assert got["Analysis"] == 6 and got["Analysis/Real Analysis"] == 2 and got["Topology"] == 1
