"""Book profiles: TOC + preface, stored and searchable."""
import json
import sqlite3

from librarian import profiles

PREFACE = ("This book is an introduction to Fourier series and the Fourier transform, written for students "
           "who know Lebesgue integration. ") * 6
OPENING = ("Chapter one. We study stochastic differential equations driven by Brownian motion, "
           "with Ito calculus throughout. ") * 8


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE books(sha256 TEXT PRIMARY KEY, path TEXT, title TEXT, author TEXT, pdf_pages INT);
    CREATE TABLE index_state(sha256 TEXT PRIMARY KEY);
    CREATE TABLE sections(sha256 TEXT, seq INT, level INT, number TEXT, title TEXT, printed_page TEXT,
                          pdf_start INT, pdf_end INT, source TEXT, verified INT);
    CREATE VIRTUAL TABLE pages_fts USING fts5(text, sha256 UNINDEXED, pdf_page UNINDEXED);
    """)
    con.executemany("INSERT INTO books VALUES(?,?,?,?,?)", [
        ("f" * 64, "Analysis/Real Analysis/Fourier - Stein.pdf", "Fourier Analysis", "Stein", 300),
        ("s" * 64, "Measure Theory/Stochastic Theory/SDE - Oksendal.pdf", "Stochastic Differential Equations", "Oksendal", 200),
        ("e" * 64, "Topology/Empty.pdf", "Empty", "Nobody", 10)])
    con.executemany("INSERT INTO index_state VALUES(?)", [("f" * 64,), ("s" * 64,), ("e" * 64,)])
    secs = [(1, 0, None, "Preface", 5, 6), (2, 1, "1", "Fourier series", 10, 40), (3, 1, "2", "The Fourier transform", 41, 80),
            (4, 1, "3", "Finite Fourier analysis", 81, 100), (5, 1, "4", "Dirichlet's theorem", 101, 120),
            (6, 1, "5", "Distributions", 121, 150), (7, 1, "6", "Bibliography", 290, 300)]
    con.executemany("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [("f" * 64, s, lv, n, t, None, a, b, "toc", 1) for s, lv, n, t, a, b in secs])
    con.execute("INSERT INTO pages_fts VALUES(?,?,?)", (PREFACE, "f" * 64, 5))
    con.execute("INSERT INTO pages_fts VALUES(?,?,?)", ("more preface", "f" * 64, 6))
    con.execute("INSERT INTO pages_fts VALUES(?,?,?)", ("short", "s" * 64, 1))
    con.execute("INSERT INTO pages_fts VALUES(?,?,?)", (OPENING, "s" * 64, 3))
    return con


def test_profile_takes_chapters_and_preface():
    con = _db()
    p = profiles.build_profile(con, "f" * 64)
    assert p["toc"][:3] == ["Preface", "1 Fourier series", "2 The Fourier transform"]   # levels 0 and 1
    assert p["intro_from"] == "Preface" and p["intro"].startswith("This book is an introduction")
    assert p["folder"] == "Analysis/Real Analysis"


def test_no_toc_falls_back_to_the_opening_pages():
    p = profiles.build_profile(_db(), "s" * 64)
    assert p["toc"] == [] and p["intro_from"] == "opening pages" and "Ito calculus" in p["intro"]


def test_store_find_load_and_dossiers(tmp_path):
    con = _db()
    got = profiles.build_all(con)
    assert got == {"profiles": 2, "without_text": 1}
    assert [b["title"] for b in profiles.find(con, "Fourier transform")] == ["Fourier Analysis"]
    assert [b["title"] for b in profiles.find(con, "Brownian")] == ["Stochastic Differential Equations"]
    assert profiles.find(con, "Fourier", shas={"s" * 64}) == []
    assert profiles.load(con, "f" * 64)["toc"][1] == "1 Fourier series"
    # Refreshing replaces, never duplicates, the searchable row.
    profiles.refresh(con, "f" * 64)
    assert con.execute("SELECT count(*) FROM profiles_fts WHERE sha256=?", ("f" * 64,)).fetchone()[0] == 1
    out = tmp_path / "d.jsonl"
    assert profiles.dossiers(con, out, intro_chars=50) == 2
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {r["title"] for r in rows} == {"Fourier Analysis", "Stochastic Differential Equations"}
    assert all(len(r["intro"]) <= 50 for r in rows)
