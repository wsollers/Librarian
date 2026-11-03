from librarian import pagemap, search, text
from librarian.index import chunk_page


def test_clean_lines_and_ligatures():
    assert text.clean_lines("\r\n\fDe\ufb01ne a\x07 set  \r\nsecond\n\n") == ["Define a set", "second"]


def test_quality_orders_clean_above_junk():
    clean = ["Define a construction sequence to be a finite sequence of expressions such that"] * 3
    junk = ["(cid:12)(cid:4)(cid:88) (cid:3)(cid:9)"] * 10
    assert text.quality(clean) > 0.8 > 0.2 > text.quality(junk)
    assert text.quality([]) == 0.0


def test_roman():
    assert [pagemap.int_to_roman(n) for n in (1, 4, 9, 14, 40)] == ["i", "iv", "ix", "xiv", "xl"]
    assert pagemap.roman_to_int("xiv") == 14 and pagemap.roman_to_int("iiii") is None


def test_detect_offset_with_front_matter():
    pages = {}
    for p in range(1, 41):
        if p <= 3:
            pages[p] = ["Title page"]
        elif p <= 10:
            pages[p] = ["Preface", "body", pagemap.int_to_roman(p - 2)]
        else:
            pages[p] = [f"{p - 10} CHAPTER ONE", "body text", "more"]
    m = pagemap.detect(pages)
    assert m[20] == "10" and m[40] == "30" and m[6] == "iv"


def test_override(tmp_path):
    (tmp_path / "abcdef0123456789.txt").write_text("1-3 none\n4-10 roman 2\n11-40 +10  # body\n")
    rules = pagemap.load_override("abcdef0123456789" + "0" * 48, tmp_path)
    m = pagemap.apply_override({p: "x" for p in range(1, 41)}, rules)
    assert m[1] is None and m[4] == "ii" and m[11] == "1" and m[40] == "30"


def test_line_span_across_break():
    lines = ["We can elaborate as follows. Define a construction", "sequence to be a finite sequence", "x"]
    assert search.line_span(lines, '"construction sequence"') == (1, 2)


def test_chunks_cover_page():
    lines = [f"line {i} with enough words to count as content here" for i in range(30)]
    ch = chunk_page(lines)
    assert ch[0][:2] == (1, 12) and ch[-1][1] == 30
