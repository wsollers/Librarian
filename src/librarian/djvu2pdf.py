"""DjVu -> PDF with the DjVu's own hidden text as an invisible, searchable text layer.

Each page: rendered with `ddjvu` (grey JPEG at --dpi), placed at the page's native size; every
word from `djvused print-txt` drawn invisibly (text render mode 3) over its box, horizontally
scaled to the box width, so selection, search and `pdftotext` line up with the image.
Needs DjVuLibre (ddjvu, djvused, djvudump), Pillow and reportlab.
"""

from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path
from typing import Optional

from .log import Timer, log

_WORD = re.compile(r'\((word|char|line)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+"((?:[^"\\]|\\.)*)"\)')
_INFO = re.compile(rb"DjVu (\d+)x(\d+),.*?(\d+) dpi")
_ESC = re.compile(r"\\([0-7]{1,3}|.)")
FONT_CANDIDATES = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _tool(name: str) -> str:
    from .extract import djvu_tool
    t = djvu_tool(name)
    if not t:
        raise SystemExit(f"{name} not found: install DjVuLibre (winget install --id DjVuLibre.DjView --exact)")
    return t


def unescape(s: str) -> str:
    """DjVu sexp strings: C escapes, octal escapes are UTF-8 bytes."""
    out = bytearray()
    i = 0
    for m in _ESC.finditer(s):
        out += s[i:m.start()].encode("utf-8")
        e = m.group(1)
        if e[0] in "01234567":
            out.append(int(e, 8) & 0xFF)
        else:
            out += {"n": "\n", "t": "\t", "r": "\r"}.get(e, e).encode("utf-8")
        i = m.end()
    out += s[i:].encode("utf-8")
    return out.decode("utf-8", "replace")


def page_infos(djvu: Path) -> list[tuple[int, int, int]]:
    """(width px, height px, dpi) for every page, in order."""
    r = subprocess.run([_tool("djvudump"), str(djvu)], capture_output=True, timeout=300)
    return [(int(w), int(h), int(d)) for w, h, d in _INFO.findall(r.stdout)]


def page_words(djvu: Path, page: int) -> list[tuple[int, int, int, int, str]]:
    r = subprocess.run([_tool("djvused"), str(djvu), "-e", f"select {page}; print-txt"],
                       capture_output=True, timeout=120)
    text = r.stdout.decode("utf-8", "replace")
    found = _WORD.findall(text)
    # Prefer the finest zones present: words, else chars, else lines.
    for kind in ("word", "char", "line"):
        zs = [(int(a), int(b), int(c), int(d), unescape(t)) for k, a, b, c, d, t in found if k == kind]
        if zs:
            return zs
    return []


def render(djvu: Path, page: int, dpi: int, out_dir: Optional[Path] = None):
    """Page image straight from ddjvu's stdout: no temporary file for antivirus to lock (WinError 32)."""
    from PIL import Image
    r = subprocess.run([_tool("ddjvu"), "-format=pnm", f"-page={page}", f"-scale={dpi}", str(djvu)],
                       check=True, capture_output=True, timeout=300)
    with Image.open(io.BytesIO(r.stdout)) as im:
        return im.convert("L")


def _font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            pdfmetrics.registerFont(TTFont("LibText", p))
            return "LibText"
    return "Helvetica"


def convert(djvu: Path, out: Path, dpi: int = 200, quality: int = 70, title: Optional[str] = None) -> dict:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    t = Timer()
    infos = page_infos(djvu)
    if not infos:
        raise SystemExit(f"no pages found in {djvu}")
    font = _font()
    tmp_out = out.with_suffix(".pdf.part")
    c = canvas.Canvas(str(tmp_out), pageCompression=1)
    c.setTitle(title or djvu.stem)
    c.setCreator("librarian djvu2pdf (DjVu hidden text as invisible text layer)")
    words_total = 0
    if True:
        for p, (w, h, native) in enumerate(infos, 1):
            W, H = w * 72 / native, h * 72 / native          # page size in points
            c.setPageSize((W, H))
            img = render(djvu, p, min(dpi, native))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            buf.seek(0)
            c.drawImage(ImageReader(buf), 0, 0, W, H)
            k = 72 / native                                   # DjVu px (origin bottom-left) -> points
            for x0, y0, x1, y1, word in page_words(djvu, p):
                word = word.strip()
                if not word or x1 <= x0 or y1 <= y0:
                    continue
                size = max(1.0, (y1 - y0) * k)
                tw = stringWidth(word, font, size) or 1.0
                to = c.beginText()
                to.setTextRenderMode(3)                       # invisible
                to.setFont(font, size)
                to.setHorizScale(100.0 * (x1 - x0) * k / tw)
                to.setTextOrigin(x0 * k, y0 * k + 0.2 * size)
                to.textOut(word)
                c.drawText(to)
                words_total += 1
            c.showPage()
            if p % 50 == 0 or p == len(infos):
                log.info("  djvu2pdf %s: %d/%d pages (%.0fs)", djvu.name, p, len(infos), t.s)
    c.save()
    tmp_out.replace(out)
    log.info("djvu2pdf: %s -> %s, %d pages, %d words, %.1f MB in %.0fs", djvu.name, out.name, len(infos),
             words_total, out.stat().st_size / 1e6, t.s)
    return {"djvu": str(djvu), "pdf": str(out), "pages": len(infos), "words": words_total,
            "seconds": round(t.s, 1)}
