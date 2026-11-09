import json
from pathlib import Path

from librarian import sections, toc


def test_contents_pages_follows_run():
    pages = {i: ["running head", "text"] * 10 for i in range(1, 40)}
    pages[5] = ["CONTENTS"] + [f"{k}. Topic {k} ..... {10 + k}" for k in range(1, 20)]
    pages[6] = [f"{k}. Topic {k}  {30 + k}" for k in range(1, 20)]
    pages[7] = [f"XI{k} Chapter  {80 + k}" for k in range(1, 9)]
    assert toc.contents_pages(pages, 12) == [5, 6, 7]


def test_contents_pages_without_heading_takes_densest():
    pages = {i: ["prose line"] * 20 for i in range(1, 30)}
    pages[3] = [f"Section {k} title {k * 3}" for k in range(1, 25)]
    assert toc.contents_pages(pages, 10)[0] == 3


def test_codex_toc_reads_json(tmp_path: Path):
    (tmp_path / "toc.json").write_text(json.dumps({"entries": [
        {"level": 1, "number": "XIX", "title": "Lie groups", "page": "1"},
        {"level": 2, "number": "19.3", "title": "Lie algebras", "page": "12"},
        {"level": 9, "number": None, "title": "Index", "page": "XII"}]}), encoding="utf-8")
    e = sections.codex_toc(tmp_path)
    assert [(x.level, x.number, x.printed) for x in e] == [(1, "XIX", "1"), (2, "19.3", "12"), (3, None, "xii")]


def test_place_unpaged_finds_mid_page_heading():
    pages = {1: ["Chapter XIX", "LIE GROUPS"], 2: ["text"] * 20 + ["3. Lie algebras of a Lie group", "more"],
             3: ["x"], 4: ["Chapter XX"]}
    es = [sections.Entry(1, "XIX", "Lie groups", "1", 1, True), sections.Entry(2, "19.3", "Lie algebras", ""),
          sections.Entry(1, "XX", "Principal connections", "4", 4, True)]
    sections.place_unpaged(es, pages)
    assert es[1].pdf == 2
