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


def test_descriptive_detection_and_def_cue():
    assert search.is_descriptive("What is the type of continuity where the change is bounded by a set constant value")
    assert not search.is_descriptive('"construction sequence"')
    assert not search.is_descriptive("Lipschitz continuous")
    assert search._DEF_CUE.search("obey the bound |f(x) - f(y)| <= M|x - y| are known as Lipschitz continuous")
    assert search._DEF_CUE.search("Define a construction sequence to be a finite sequence")
    assert not search._DEF_CUE.search("Using the definition of continuity directly prove that f is defined by")


class _FakeReranker:
    """Scores a passage by how many query words it contains (stands in for the cross-encoder)."""

    def scores(self, q, passages):
        qs = set(q.lower().split())
        return [float(len(qs & set(p.lower().replace(".", " ").split()))) - 2 for p in passages]


def test_refine_pins_the_defining_lines():
    lines = ["filler line"] * 10 + [
        "Functions which obey the bound |f(x) - f(y)| <= M|x - y| are known as",
        "Lipschitz continuous functions with Lipschitz constant M.",
    ] + ["more filler"] * 10
    c = search.Cand("s", 1, (5, 16))
    c.region, c.rerank = (1, len(lines)), 0.5
    search.refine([c], {("s", 1): lines}, "lipschitz continuous functions constant", _FakeReranker())
    a, b = c.span
    assert a <= 12 <= b and b - a + 1 == search.WINDOW


def test_sentence_span_completes_definition():
    lines = ["the derivative is bounded.", "Functions which obey the bound |f(x) - f(y)| <= M|x - y| are known as",
             "Lipschitz continuous functions with Lipschitz constant M; thus this", "exercise shows the claim.", "Next."]
    assert search.sentence_span(lines, 1, 2) == (1, 4)
    assert search.sentence_span(lines, 3, 3) == (2, 4)


def test_toc_detection():
    toc = [f"9.{i} Some section . . . . . . . . . . {230 + i}" for i in range(10)]
    body = ["Definition 9.9.2 (Uniform continuity). Let X be a subset of R."] * 10
    assert search.is_toc_page(toc) and not search.is_toc_page(body)


def test_nougat_ranges_and_similarity():
    from librarian import compare, ocr
    assert ocr._ranges([5, 1, 2, 3, 9, 10]) == "1-3,5,9-10"
    assert compare.similarity(r"Let $f:\mathbb R\to\mathbb R$ be continuous", "Let $f: R \\to R$ be continuous") > 0.9
    assert compare.similarity("completely different words here", "nothing alike at all") < 0.3


def test_toc_entry_formats():
    from librarian.sections import parse_entry as P
    e = P("  1.1 What is analysis? . . . . . . . . . . . . . . .       1")
    assert (e.level, e.number, e.title, e.printed) == (2, "1.1", "What is analysis?", "1")
    e = P("       CHAPTER ONE       Sentential Logic             11")
    assert (e.level, e.number, e.title, e.printed) == (1, "1", "Sentential Logic", "11")
    e = P("Preface to the \ufb01rst edition                                    xiii")
    assert (e.level, e.number, e.title, e.printed) == (1, None, "Preface to the first edition", "xiii")
    e = P("| 3.4 | Uniform continuity | 138 |")
    assert (e.number, e.title, e.printed) == ("3.4", "Uniform continuity", "138")
    assert P("viii                                       Contents") is None


def test_printed_to_pdf_and_locate(tmp_path):
    import sqlite3
    from librarian import sections
    mapping = {p: (str(p - 13) if p > 13 else None) for p in range(1, 60)}
    assert sections.printed_to_pdf("17", mapping) == 30
    con = sqlite3.connect(":memory:")
    con.executescript(sections.SCHEMA)
    con.executemany("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?)", [
        ("s", 0, 1, "1", "Sentential Logic", "11", 24, 79, "toc", 1),
        ("s", 1, 2, "1.1", "The Language of Sentential Logic", "13", 26, 32, "toc", 1)])
    assert sections.locate(con, "s", 30) == {"chapter": "1 Sentential Logic", "section": "1.1 The Language of Sentential Logic"}
    assert sections.locate(con, "s", 5) == {}


def test_damage_patterns():
    from librarian import audit, text
    damaged = "for each \u03b5 A \uf639, there is a \u03b4 A \uf639 such that"
    brace = "g(\u03c3) = \uf8f1\n\uf8f4 0 if |\u03c3| = 0\n\uf8f2 1 if |\u03c3| = 1"
    assert len(audit.PUA.findall(damaged)) == 2 and not audit.PUA.findall(brace)
    assert text.quality([damaged] * 5) < text.quality([brace.replace("\n", " ")] * 5)
