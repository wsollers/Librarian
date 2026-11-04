"""Score OCR outputs against the born-digital ground truth.

Layout:  SET/truth/pNNNN.txt   OUT/<engine>/<level>/pNNNN.md (or .txt)   OUT/<engine>/<level>/timing.json
Every output is reduced to the same plain form (LaTeX -> Unicode, Markdown stripped, NFKC,
lower case) before comparison, so LaTeX-emitting engines are not penalised for notation.

Metrics per engine x level (pages pooled):
  cer      character error rate on the letters+digits stream (order-sensitive)
  w_rec    prose word recall (words >= 3 letters, multiset)  -> dropped text
  w_prec   prose word precision                              -> hallucinated / garbage text
  sym_f1   F1 on math symbols (Greek, relations, set ops, arrows, brackets)
  lines    share of printed lines (>= 3 words) whose first three words start a line of the output;
           librarian cites line numbers, so engines that reflow paragraphs score low here
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

try:
    from rapidfuzz.distance import Levenshtein
    lev = Levenshtein.distance
except ImportError:  # pure-python fallback (pages are a few thousand chars)
    def lev(a: str, b: str) -> int:
        if len(a) < len(b):
            a, b = b, a
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

# Small, deterministic LaTeX -> Unicode reduction (same on every machine; no pylatexenc).
_CMD = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇", "cup": "∪",
    "cap": "∩", "emptyset": "∅", "varnothing": "∅", "neq": "≠", "ne": "≠", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "leqq": "≤", "geqq": "≥", "cong": "≅", "equiv": "≡", "sim": "∼", "to": "→", "rightarrow": "→",
    "mapsto": "↦", "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒", "implies": "⇒", "Leftrightarrow": "⇔",
    "iff": "⇔", "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "wedge": "∧", "land": "∧", "vee": "∨",
    "lor": "∨", "times": "×", "cdot": "·", "circ": "∘", "pm": "±", "infty": "∞", "sum": "∑", "prod": "∏",
    "int": "∫", "sqrt": "√", "ldots": "...", "cdots": "...", "dots": "...", "vdots": "", "ddots": "",
    "lbrace": "\x01", "rbrace": "\x02", "{": "\x01", "}": "\x02", "langle": "<", "rangle": ">", "mid": "|", "vert": "|",
    "colon": ":", "quad": " ", "qquad": " ", ",": " ", ";": " ", "!": "", " ": " ", "\\\\": "\n",
    "blacksquare": "■", "square": "□", "Box": "□", "qed": "■", "prime": "'", "setminus": "\\",
}
_UNWRAP = re.compile(r"\\(?:mathbb|mathcal|mathrm|mathbf|mathit|mathsf|mathfrak|text|textbf|textit|emph|operatorname|"
                     r"boldsymbol|bm|mbox|textrm|hat|bar|tilde|overline|underline|left|right|big|Big|bigg|Bigg)\b\s*")


def latex_plain(t: str) -> str:
    t = re.sub(r"\\begin\{[a-z*]+\}(\{[^}]*\})?|\\end\{[a-z*]+\}", " ", t)
    t = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1/\2", t)
    t = _UNWRAP.sub("", t)
    t = re.sub(r"\\([A-Za-z]+|[{},;! ]|\\\\)", lambda m: _CMD.get(m.group(1), ""), t)
    t = t.replace("&", " ").replace("$", "")
    t = t.replace("{", "").replace("}", "")          # grouping braces
    return t.replace("\x01", "{").replace("\x02", "}")


MATH = set("αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩϕϵ∈∉⊂⊆⊃⊇∪∩∅≠≤≥≅≡∼→↦←↔⇒⇔∀∃¬∧∨×·∘±∞∑∏∫√=<>+()[]{}|")
_LATEX_HINT = re.compile(r"\\[a-zA-Z]+|\$|\\\(|\\\[")


def plain(text: str) -> str:
    t = text.replace("\r", "")
    t = re.sub(r"```[a-z]*", "", t)
    if _LATEX_HINT.search(t):
        t = t.replace("\\(", " ").replace("\\)", " ").replace("\\[", " ").replace("\\]", " ")
        t = latex_plain(t)
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


def line_starts(raw: str) -> set:
    out = set()
    for ln in raw.splitlines():
        w = re.findall(r"[a-z]{3,}", plain(ln))
        if len(w) >= 3:
            out.add(" ".join(w[:3]))
    return out


def overlap(a: Counter, b: Counter) -> int:
    return sum((a & b).values())


def read_out(d: Path, stem: str) -> str:
    for ext in (".md", ".mmd", ".txt"):
        f = d / (stem + ext)
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


def score_dir(truth: dict, d: Path, raw: dict) -> dict:
    lh = lt = 0
    ed = tl = 0
    tw = ow = mw = 0
    ts = os_ = ms = 0
    bad = []
    for stem, tt in truth.items():
        ro = read_out(d, stem)
        o = plain(ro)
        T, O = line_starts(raw[stem]), line_starts(ro)
        lh, lt = lh + len(T & O), lt + len(T)
        ta, oa = alnum(tt), alnum(o)
        ed += lev(ta, oa)
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
           "sym_f1": 2 * p * r / (p + r) if p + r else 0, "lines": lh / lt if lt else 0, "bad_pp": len(bad), "bad": bad}
    tj = d / "timing.json"
    if tj.is_file():
        t = json.loads(tj.read_text())
        res["s_per_page"] = t.get("seconds", 0) / max(1, t.get("pages", len(truth)))
    return res


def main():
    set_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    raw = {f.stem: f.read_text(encoding="utf-8") for f in sorted((set_dir / "truth").glob("p*.txt"))}
    truth = {k: plain(v) for k, v in raw.items()}
    rows = []
    for eng in sorted(p for p in out.iterdir() if p.is_dir()):
        for lvl in sorted(p for p in eng.iterdir() if p.is_dir()):
            r = score_dir(truth, lvl, raw)
            rows.append({"engine": eng.name, "level": lvl.name, **r})
    rows.sort(key=lambda r: (r["level"], r["cer"]))
    hdr = f"{'engine':<22}{'level':<6}{'CER':>7}{'w_rec':>7}{'w_prec':>8}{'sym_F1':>8}{'lines':>7}{'bad_pp':>8}{'s/page':>8}"
    print(hdr)
    for r in rows:
        sp = f"{r['s_per_page']:.1f}" if "s_per_page" in r else "-"
        print(f"{r['engine']:<22}{r['level']:<6}{r['cer']:7.3f}{r['w_rec']:7.3f}{r['w_prec']:8.3f}"
              f"{r['sym_f1']:8.3f}{r['lines']:7.2f}{r['bad_pp']:8d}{sp:>8}")
    if "--csv" in sys.argv:
        import csv
        with open(sys.argv[sys.argv.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["s_per_page"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    (out / "scores.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
