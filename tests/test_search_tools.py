import sqlite3

import numpy as np

from librarian import search, spell
from librarian.index import VectorStore


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE books(sha256 TEXT PRIMARY KEY, path TEXT, title TEXT, author TEXT);
    CREATE TABLE sections(sha256 TEXT, seq INT, level INT, number TEXT, title TEXT, printed_page TEXT,
                          pdf_start INT, pdf_end INT, source TEXT, verified INT);
    CREATE VIRTUAL TABLE pages_fts USING fts5(text, sha256 UNINDEXED, pdf_page UNINDEXED,
                                              tokenize='unicode61 remove_diacritics 2');
    """)
    con.executemany("INSERT INTO books VALUES(?,?,?,?)", [
        ("aaaa" * 16, "Analysis/Tao I.pdf", "Analysis I", "Terence Tao"),
        ("bbbb" * 16, "Analysis/Rudin.pdf", "Principles of Mathematical Analysis", "Walter Rudin")])
    con.executemany("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?)", [
        ("aaaa" * 16, 1, 1, "5", "The real numbers", "95", 100, 140, "toc", 1),
        ("aaaa" * 16, 2, 2, "5.4", "Ordering the reals", "110", 115, 120, "toc", 1),
        ("bbbb" * 16, 1, 1, "4", "Continuity", "83", 90, 110, "toc", 1),
        ("bbbb" * 16, 2, 2, "4.1", "Limits of functions", "83", 90, 95, "toc", 1)])
    for i in range(5):
        con.execute("INSERT INTO pages_fts VALUES(?,?,?)", (f"f is Lipschitz continuous with constant K, page {i}", "a" * 64, i))
    con.execute("INSERT INTO pages_fts VALUES('Hölder continuity','x',1)")
    return con


def test_osa_distance():
    assert spell.osa("lipsctiz", "lipschitz", 2) == 2
    assert spell.osa("abcd", "abdc", 2) == 1
    assert spell.osa("abc", "xyzuvw", 2) == 3


def test_spell_corrects_only_unknown_words():
    con = _db()
    v = spell.Vocab(con)
    q, fixes = v.correct("lipsctiz continuity", search.STOP)
    assert fixes == {"lipsctiz": "lipschitz"} and q == "lipschitz continuity"
    assert v.correct("lipschitz constant", search.STOP) == ("lipschitz constant", {})
    assert spell.fold("Hölder") == "holder"


def test_scope_by_author_chapter_section():
    con = _db()
    a, b = "aaaa" * 16, "bbbb" * 16
    assert search.scope(con) is None
    assert search.scope(con, author="Tao") == {a: None}
    assert search.scope(con, author="Nobody") == {}
    assert search.scope(con, chapter="5", author="tao") == {a: [(100, 140)]}
    assert search.scope(con, chapter="Chapter 4") == {b: [(90, 110)]}
    assert search.scope(con, section="5.4") == {a: [(115, 120)]}
    assert search.scope(con, chapter="Continuity", section="Limits") == {b: [(90, 95)]}
    assert search.in_scope({a: [(100, 140)]}, a, 120) and not search.in_scope({a: [(100, 140)]}, a, 99)


def test_knn_among_blob_store():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE chunks_blob (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    vs = np.eye(4, dtype=np.float32)
    con.executemany("INSERT INTO chunks_blob VALUES(?,?)", [(i + 1, vs[i].tobytes()) for i in range(4)])
    store = VectorStore(con, 4, "hash:4", create=False)
    store.vec = False
    got = store.knn_among(np.array([0, 0, 1, 0], dtype=np.float32), [1, 3, 4], 2)
    assert got[0] == (3, 1.0) and len(got) == 2


def test_spell_prefers_the_common_word_over_a_closer_rare_one():
    con = _db()
    for i in range(2):
        con.execute("INSERT INTO pages_fts VALUES('lipschiz is a misprint',?,?)", ("c" * 64, 10 + i))
    con.execute("INSERT INTO pages_fts VALUES('lipschiz again','d',1)")
    v = spell.Vocab(con)
    v.by_key.setdefault(("l", 9), []).append(("lipschitz", 3753))    # as in the real library
    v.by_key.setdefault(("l", 8), []).append(("lipschiz", 4))
    assert v.suggest("lipsctiz") == "lipschitz"
