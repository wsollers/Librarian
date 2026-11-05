"""Spelling correction against the index's own vocabulary (FTS5 terms), no model.

A query word is corrected only when the library barely knows it (in fewer than MIN_DOC pages)
and a word within edit distance 1-2 is far more common there: "lipsctiz" -> "lipschitz".
Words the books really use are never "corrected" into something more common.
The word list (terms in >= MIN_DOC pages) is cached next to the index, keyed by its size and mtime.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Optional

from .log import log

MIN_DOC = 3          # a term in fewer pages is "unknown" (OCR junk lives here too)
RATIO = 20           # a correction must be this many times more common than the typed word
EDIT_COST = 3.0      # one more edit must be worth ~e^3 = 20x more pages ("lipschitz" beats the rarer "lipschiz")
_WORD = re.compile(r"\w+", re.UNICODE)
_ALPHA = re.compile(r"^[a-z]+$")


def fold(word: str) -> str:
    """As the unicode61 tokenizer with remove_diacritics: lower case, no accents."""
    w = unicodedata.normalize("NFKD", word.lower())
    return "".join(ch for ch in w if not unicodedata.combining(ch))


def osa(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (Damerau with adjacent transpositions), early exit past limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = i
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


class Vocab:
    def __init__(self, con: sqlite3.Connection, db_path: Optional[Path] = None, cache_dir: Optional[Path] = None):
        self.con = con
        self._vocab_table(con)
        self.by_key: dict[tuple[str, int], list[tuple[str, int]]] = {}   # (first letter, length) -> [(term, doc)]
        terms = self._load(db_path, cache_dir)
        for t, d in terms:
            self.by_key.setdefault((t[0], len(t)), []).append((t, d))
        log.debug("spell: %d words", len(terms))

    @staticmethod
    def _vocab_table(con: sqlite3.Connection) -> None:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.pages_vocab USING fts5vocab(main, pages_fts, 'row')")

    def _load(self, db_path, cache_dir) -> list[tuple[str, int]]:
        stamp = None
        cache = None
        if db_path and cache_dir and db_path.is_file():
            st = db_path.stat()
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
            cache = cache_dir / "vocab.json"
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("stamp") == stamp:
                    return [tuple(x) for x in data["terms"]]
            except (OSError, ValueError, KeyError):
                pass
        log.info("spell: building the word list from the index (once per index build)")
        terms = [(t, d) for t, d in self.con.execute("SELECT term, doc FROM temp.pages_vocab WHERE doc >= ?", (MIN_DOC,))
                 if _ALPHA.match(t) and len(t) >= 3]
        if cache:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"stamp": stamp, "terms": terms}), encoding="utf-8")
            except OSError as e:
                log.warning("spell: cannot write %s: %s", cache, e)
        return terms

    def doc(self, term: str) -> int:
        r = self.con.execute("SELECT doc FROM temp.pages_vocab WHERE term = ?", (term,)).fetchone()
        return r[0] if r else 0

    def suggest(self, word: str) -> Optional[str]:
        w = fold(word)
        if len(w) < 4 or not _ALPHA.match(w):
            return None
        have = self.doc(w)
        if have >= MIN_DOC:
            return None
        limit = 1 if len(w) <= 5 else 2
        best: Optional[tuple[float, str]] = None   # (score, term): common and close wins
        for first in {w[0], w[1]}:   # also the first two letters swapped ("ilpschitz")
            for n in range(len(w) - limit, len(w) + limit + 1):
                for t, d in self.by_key.get((first, n), ()):
                    if d < max(MIN_DOC, RATIO * have):
                        continue
                    dist = osa(w, t, limit)
                    if dist <= limit:
                        score = math.log(d) - EDIT_COST * dist
                        if best is None or score > best[0]:
                            best = (score, t)
        return best[1] if best else None

    def correct(self, q: str, stop: set[str] = frozenset()) -> tuple[str, dict[str, str]]:
        """Return the query with unknown words replaced, and {typed: corrected}."""
        fixes: dict[str, str] = {}
        for m in _WORD.finditer(q):
            tok = m.group(0)
            if tok.lower() in stop or tok in fixes:
                continue
            s = self.suggest(tok)
            if s and s != fold(tok):
                fixes[tok] = s
        if not fixes:
            return q, {}
        out = _WORD.sub(lambda m: fixes.get(m.group(0), m.group(0)), q)
        return out, fixes
