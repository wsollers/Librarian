"""Text cleaning and page-quality scoring."""

from __future__ import annotations

import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CID = re.compile(r"\(cid:\d+\)")
# Typographic ligatures break search ("Deﬁne" != "Define"); math alphanumerics are kept as-is.
_LIGATURES = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
                            "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"})
_WORD = re.compile(r"[A-Za-z]{2,}")
_PUA = re.compile("[\ue000-\uf8e4]")  # U+F8E5-F8FF are big-delimiter pieces, not damage


def clean_lines(raw: str) -> list[str]:
    """Normalise one page of extracted text into stable lines (LF, no control chars)."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "")
    raw = unicodedata.normalize("NFC", raw).translate(_LIGATURES)
    lines = [_CTRL.sub("", ln).rstrip() for ln in raw.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


SPARSE_CHARS = 40


def nonspace(lines: list[str]) -> int:
    return sum(1 for ln in lines for c in ln if not c.isspace())


def is_confirmed_sparse(method: str, lines: list[str]) -> bool:
    """OCR looked at the page image and found almost nothing: blank verso with a page number,
    part title, figure. Such a page is complete, not low quality."""
    return method.startswith("llm-ocr") and nonspace(lines) < SPARSE_CHARS


def quality(lines: list[str]) -> float:
    """0..1 heuristic: 0 = no text, low = garbled/encoding junk, ~1 = clean prose/math."""
    text = "\n".join(lines)
    nonspace = sum(1 for c in text if not c.isspace())
    if nonspace < 40:
        return 0.0
    letters = sum(1 for c in text if c.isalpha())
    # replacement chars, (cid:N) and private-use glyphs = font-encoding damage (symbols lost)
    bad = text.count("\ufffd") + 8 * len(_CID.findall(text)) + 4 * len(_PUA.findall(text))
    words = _WORD.findall(text)
    vowelish = sum(1 for w in words if re.search(r"[aeiouyAEIOUY]", w))
    word_ok = vowelish / len(words) if words else 0.0
    letter_ratio = letters / nonspace
    penalty = min(1.0, bad / max(1, nonspace) * 10)
    return round(max(0.0, (0.5 * min(1.0, letter_ratio / 0.6) + 0.5 * word_ok) * (1 - penalty)), 3)
