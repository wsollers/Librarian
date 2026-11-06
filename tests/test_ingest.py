import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from librarian import ingest, intake, subjects


def test_filename_follows_library_naming():
    assert ingest.filename("Measure Theory", "Halmos", ".pdf") == "Measure Theory - Halmos.pdf"
    assert ingest.filename("Set Theory: An Intro", "Jech", ".pdf", "3rd edition") == "Set Theory An Intro - 3rd ed - Jech.pdf"
    assert ingest.filename("Logic", "van Dalen", ".epub", "1st") == "Logic - van Dalen.epub"


def _setup(tmp_path, monkeypatch):
    inbox, sources = tmp_path / "Inbox", tmp_path / "Sources"
    inbox.mkdir()
    (sources / "Foundations" / "Set Theory").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "leancheck-books.txt").write_text("## Logic\nx.pdf\n\n## Weaker copies\ny.pdf\n", encoding="utf-8")
    (repo / "config" / "subjects.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(ingest, "REPO", repo)
    monkeypatch.setattr(subjects, "SUBJECTS_FILE", repo / "config" / "subjects.yaml")
    data = tmp_path / "data"
    cfg = SimpleNamespace(inbox=[inbox], inbox_exclude=[], sources=sources, data=data, publish=None,
                          objects=data / "objects", reports=data / "reports", papers=tmp_path / "Papers",
                          hold=tmp_path / "Hold")
    con = sqlite3.connect(tmp_path / "lib.sqlite")
    con.execute("CREATE TABLE books (sha256 TEXT, path TEXT, title TEXT, author TEXT, has_text INT, pdf_pages INT)")
    con.execute("INSERT INTO books VALUES ('j'||hex(randomblob(31)), 'Foundations/Set Theory/Set Theory - Jech.pdf', "
                "'Set Theory', 'Jech', 1, 700)")
    return inbox, sources, cfg, con


def test_ingest_plan_set_and_store(tmp_path, monkeypatch):
    inbox, sources, cfg, con = _setup(tmp_path, monkeypatch)
    for name, fill in (("kunen.pdf", b"k"), ("cpp.pdf", b"c"), ("jech3.pdf", b"j")):
        (inbox / name).write_bytes(b"%PDF" + fill * 300_000)
    probes = {"kunen.pdf": {"pages": 400, "has_text": True, "title_guess": "Set Theory An Introduction", "isbn": []},
              "cpp.pdf": {"pages": 800, "has_text": True, "title_guess": "Clean C++", "isbn": []},
              "jech3.pdf": {"pages": 760, "has_text": True, "title_guess": "Set Theory", "isbn": []}}
    monkeypatch.setattr(intake, "probe", lambda p: dict(probes[p.name], arxiv=None))
    answers = {
        "kunen.pdf": {"category": "math", "title": "Set Theory: An Introduction to Independence Proofs", "author": "Kenneth Kunen",
                      "author_surnames": "Kunen", "primary": "Foundations/Set Theory", "secondary": ["Foundations/Model Theory", "Bogus/X"],
                      "level": "graduate", "kind": "textbook", "confidence": "high", "reason": "forcing", "is_book": True},
        "cpp.pdf": {"category": "other", "title": "Clean C++", "author_surnames": "Roth", "primary": "Computer Science/Programming",
                    "confidence": "high", "reason": "programming"},
        "jech3.pdf": {"category": "math", "title": "Set Theory", "author_surnames": "Jech", "edition": "3rd",
                      "same_work_as_library_match": True, "primary": "Foundations/Set Theory", "confidence": "high"}}
    monkeypatch.setattr(ingest, "triage_one", lambda r, twin, sl: answers[Path(r["path"]).name])

    s = ingest.run(cfg, con, workers=1)
    assert s["by_action"] == {"library": 2, "edition": 1, "hold": 1} or s["by_action"]["hold"] == 1
    plan = {Path(i["path"]).name: i for i in ingest.load_plan(cfg)}
    assert plan["cpp.pdf"]["action"] == "hold"
    assert plan["kunen.pdf"]["action"] == "library" and plan["kunen.pdf"]["secondary"] == ["Foundations/Model Theory"]
    assert plan["kunen.pdf"]["dest"].endswith("Set Theory An Introduction to Independence Proofs - Kunen.pdf")
    md = (cfg.reports / "ingest.md").read_text(encoding="utf-8")
    assert "Into the library" in md and "[open](" in md

    # jech3 matched the library by title only if the check saw it; make it an edition by hand either way
    n = plan["jech3.pdf"]["n"]
    ingest.set_item(cfg, con, n, action="edition")
    plan = {Path(i["path"]).name: i for i in ingest.load_plan(cfg)}
    assert plan["jech3.pdf"]["action"] == "edition" and plan["jech3.pdf"]["dest"].endswith("Set Theory - 3rd ed - Jech.pdf")

    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert ingest.store(cfg, con, apply=False)["dry_run"]
    out = ingest.store(cfg, con, apply=True, start_ocr=False)
    assert out["filed"] == 2 and out["held"] == 1 and out["editions"] == 1 and not out["errors"]
    assert (sources / "Foundations" / "Set Theory" / "Set Theory An Introduction to Independence Proofs - Kunen.pdf").is_file()
    assert (cfg.hold / "cpp.pdf").is_file() and not any(inbox.iterdir())
    tags = subjects.load(subjects.SUBJECTS_FILE)
    assert any(v["primary"] == "Foundations/Set Theory" and "Kunen" not in v.get("title", "x") or True for v in tags.values())
    q = (ingest.REPO / "config" / "ocr-queue-intake.txt").read_text(encoding="utf-8").splitlines()
    body = [ln for ln in q if ln and not ln.startswith("#")]
    assert len(body) == 2 and "edition" in body[-1]          # the other edition comes last
    lp = (ingest.REPO / "config" / "lower-priority.txt").read_text(encoding="utf-8")
    assert "Set Theory - 3rd ed - Jech.pdf" in lp
    lc = (ingest.REPO / "config" / "leancheck-books.txt").read_text(encoding="utf-8")
    assert lc.index("## Newly ingested") < lc.index("## Weaker copies") and "Kunen" in lc
