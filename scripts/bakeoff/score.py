"""Score OCR outputs against the born-digital ground truth.

Layout:  SET/truth/pNNNN.txt   OUT/<engine>/<level>/pNNNN.md (or .txt)   OUT/<engine>/<level>/timing.json
Every output is reduced to the same plain form (LaTeX -> Unicode, Markdown stripped, NFKC,
lower case) before comparison, so LaTeX-emitting engines are not penalised for notation.

Metrics per engine x level (pages pooled):
  cer      character error rate on the letters+digits stream (order-sensitive)
  w_rec    prose word recall (words >= 3 letters, multiset)  -> dropped text
  w_prec   prose word precision                              -> hallucinated / garbage text
  sym_f1   F1 on math symbols (Greek, relations, set ops, arrows, brackets)
  bad_pp   pages with w_rec < 0.80 (page-level failures: loops, truncation, blank)
  s/page   wall seconds per page if timing.json exists

usage: python score.py SET OUT [--csv results.csv]
"""
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from rapidfuzz.distance import Levenshtein

try:
    from pylatexenc.latex2text import LatexNodes2Text
    _L2T = LatexNodes2Text(math_mode="text", strict_latex_spaces=False)
except ImportError:  # pragma: no cover
    _L2T = None

MATH = set("αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩϕϵ∈∉⊂⊆⊃⊇∪∩∅≠≤≥≅≡∼→↦←↔⇒⇔∀∃¬∧∨×·∘±∞∑∏∫√=<>+()[]{}|")
_LATEX_HINT = re.compile(r"\\[a-zA-Z]+|\$|\\\(|\\\[")


def plain(text: str) -> str:
    t = text.replace("\r", "")
    t = re.sub(r"```[a-z]*", "", t)
    if _L2T and _LATEX_HINT.search(t):
        # math delimiters pylatexenc may not know
        t = t.replace("\\(", "$").replace("\\)", "$").replace("\\[", "$$").replace("\\]", "$$")
        try:
            t = _L2T.latex_to_text(t)
        except Exception:
            pass
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)          # headings
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])", r"\1", t)  # _italic_
    t = t.replace("\u0338=", "≠").replace("=\u0338", "≠").replace("̸=", "≠")
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("−", "-").replace("–", "-").replace("’", "'").replace("ϕ", "φ").replace("ϵ", "ε")
    t = t.replace("_", "").replace("^", "")
    return t.lower()


def alnum(t: str) -> str:
    return "".join(c for c in t if c.isalnum() and c.isascii())


def words(t: str) -> Counter:
    return Counter(re.findall(r"[a-z]{3,}", t))


def syms(t: str) -> Counter:
    return Counter(c for c in t if c in MATH)


def overlap(a: Counter, b: Counter) -> int:
    return sum((a & b).values())


def read_out(d: Path, stem: str) -> str:
    for ext in (".md", ".mmd", ".txt"):
        f = d / (stem + ext)
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


def score_dir(truth: dict, d: Path) -> dict:
    ed = tl = 0
    tw = ow = mw = 0
    ts = os_ = ms = 0
    bad = []
    for stem, tt in truth.items():
        o = plain(read_out(d, stem))
        ta, oa = alnum(tt), alnum(o)
        ed += Levenshtein.distance(ta, oa)
        tl += len(ta)
        a, b = words(tt), words(o)
        m = overlap(a, b)
        tw, ow, mw = tw + sum(a.values()), ow + sum(b.values()), mw + m
        if sum(a.values()) and m / sum(a.values()) < 0.8:
            bad.append(stem)
        a, b = syms(tt), syms(o)
        ts, os_, ms = ts + sum(a.values()), os_ + sum(b.values()), ms + overlap(a, b)
    p = ms / os_ if os_ else 0
    r = ms / ts if ts else 0
    res = {"cer": ed / tl if tl else 1, "w_rec": mw / tw if tw else 0, "w_prec": mw / ow if ow else 0,
           "sym_f1": 2 * p * r / (p + r) if p + r else 0, "bad_pp": len(bad), "bad": bad}
    tj = d / "timing.json"
    if tj.is_file():
        t = json.loads(tj.read_text())
        res["s_per_page"] = t.get("seconds", 0) / max(1, t.get("pages", len(truth)))
    return res


def main():
    set_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    truth = {f.stem: plain(f.read_text(encoding="utf-8")) for f in sorted((set_dir / "truth").glob("p*.txt"))}
    rows = []
    for eng in sorted(p for p in out.iterdir() if p.is_dir()):
        for lvl in sorted(p for p in eng.iterdir() if p.is_dir()):
            r = score_dir(truth, lvl)
            rows.append({"engine": eng.name, "level": lvl.name, **r})
    rows.sort(key=lambda r: (r["level"], r["cer"]))
    hdr = f"{'engine':<22}{'level':<6}{'CER':>7}{'w_rec':>7}{'w_prec':>8}{'sym_F1':>8}{'bad_pp':>8}{'s/page':>8}"
    print(hdr)
    for r in rows:
        sp = f"{r['s_per_page']:.1f}" if "s_per_page" in r else "-"
        print(f"{r['engine']:<22}{r['level']:<6}{r['cer']:7.3f}{r['w_rec']:7.3f}{r['w_prec']:8.3f}"
              f"{r['sym_f1']:8.3f}{r['bad_pp']:8d}{sp:>8}")
    if "--csv" in sys.argv:
        import csv
        with open(sys.argv[sys.argv.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["s_per_page"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    (out / "scores.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
