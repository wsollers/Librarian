from pathlib import Path

from librarian import ocr


def test_codex_pages_are_cached_and_reused(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "codex_ocr", lambda pdf, page: calls.append(page) or f"page {page}\n$x^2$")
    cache = tmp_path / "ocr-cache"
    assert ocr.llm_ocr(Path("b.pdf"), 7, cache) == ("page 7\n$x^2$", "llm-ocr:codex")
    assert (cache / "0007.md").read_text(encoding="utf-8") == "page 7\n$x^2$"
    assert ocr.llm_ocr(Path("b.pdf"), 7, cache) == ("page 7\n$x^2$", "llm-ocr:codex")
    assert calls == [7]   # second call came from the cache


def test_failed_pages_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "codex_ocr", lambda pdf, page: None)
    cache = tmp_path / "ocr-cache"
    assert ocr.llm_ocr(Path("b.pdf"), 3, cache) == (None, "llm-ocr:codex")
    assert not (cache / "0003.md").exists()


def test_codex_narration_is_not_page_text():
    assert ocr.strip_narration("I’ll inspect the page at full resolution to distinguish printed marks.") == ""
    assert ocr.strip_narration("I’m checking the page for faint text.\n\n42  Chapter 3\nText") == "42  Chapter 3\nText"
    assert ocr.strip_narration("I will not prove that every set is finite.") == "I will not prove that every set is finite."
