"""Phase 2 extract: one line-stable Markdown file per page, with per-page escalation.

objects/<sha256>/pages/NNNN.md   page text, LF, line i of the file is line i of the page
objects/<sha256>/extract.json    per-page method, quality, line count; extractor version
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import ocr
from .config import Config
from .db import now
from .log import Timer, log
from .text import clean_lines, quality

EXTRACTOR_VERSION = 2   # 2: ligatures expanded
Q_MIN = 0.45            # below this a page is escalated
_LEGACY_PAGE = re.compile(r"^=== Page (\d+) ===\s*$", re.M)


@dataclass
class Options:
    force: bool = False
    use_legacy: bool = True
    nougat: bool = False
    llm_ocr: bool = False
    q_min: float = Q_MIN


@dataclass
class PageResult:
    lines: list[str]
    method: str
    quality: float
    notes: list[str] = field(default_factory=list)


# ---------- source readers ----------

_DJVU_DIRS = [os.environ.get("LIBRARIAN_DJVULIBRE", ""), r"C:\Program Files (x86)\DjVuLibre",
              r"C:\Program Files\DjVuLibre"]


def djvu_tool(name: str) -> Optional[str]:
    """djvutxt/djvused from PATH, $LIBRARIAN_DJVULIBRE, or the winget DjVuLibre install dir."""
    found = shutil.which(name)
    if found:
        return found
    for d in _DJVU_DIRS:
        for exe in (name, name + ".exe"):
            if d and (Path(d) / exe).is_file():
                return str(Path(d) / exe)
    return None

def pdf_pages_text(pdf: Path, pages: int) -> list[str]:
    """Whole-book pdftotext split on form feeds; per-page fallback if the split is off."""
    r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=600)
    parts = r.stdout.decode("utf-8", "replace").split("\f")
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    if len(parts) == pages:
        return parts
    log.info("  form-feed split gave %d pages, expected %d: extracting page by page", len(parts), pages)
    out = []
    for p in range(1, pages + 1):
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-f", str(p), "-l", str(p), str(pdf), "-"],
                           capture_output=True, timeout=120)
        out.append(r.stdout.decode("utf-8", "replace"))
    return out


def page_has_images(pdf: Path, page: int) -> bool:
    if not shutil.which("pdfimages"):
        return True
    r = subprocess.run(["pdfimages", "-list", "-f", str(page), "-l", str(page), str(pdf)],
                       capture_output=True, timeout=60)
    return len(r.stdout.decode(errors="replace").strip().splitlines()) > 2


def djvu_pages_text(djvu: Path) -> list[str]:
    n = int(subprocess.run([djvu_tool("djvused"), "-e", "n", str(djvu)], capture_output=True, text=True,
                           timeout=60).stdout.strip())
    return [subprocess.run([djvu_tool("djvutxt"), f"--page={p}", str(djvu)], capture_output=True,
                           timeout=120).stdout.decode("utf-8", "replace") for p in range(1, n + 1)]


def epub_sections_text(epub: Path) -> list[str]:
    """EPUB has no pages: each spine document becomes one 'page'."""
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        container = z.read("META-INF/container.xml").decode()
        opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = z.read(opf_path).decode("utf-8", "replace")
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        items = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', opf))
        items.update({i: h for h, i in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', opf)})
        spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
        out = []
        for idref in spine:
            href = base + items.get(idref, "")
            if href not in names:
                continue
            doc = z.read(href).decode("utf-8", "replace")
            doc = re.sub(r"(?is)<(script|style).*?</\1>", "", doc)
            doc = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", doc)
            out.append(html.unescape(re.sub(r"<[^>]+>", "", doc)))
        return out


def legacy_pages(cfg: Config, seed_markdown: Optional[str], pages: int) -> dict[int, list[str]]:
    """Legacy lra-pdf-extractor Markdown, trusted only if its page markers cover the whole book."""
    if not seed_markdown or not cfg.legacy:
        return {}
    path = cfg.legacy / seed_markdown
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    marks = list(_LEGACY_PAGE.finditer(text))
    if not marks or int(marks[-1].group(1)) != pages:
        log.debug("  legacy markdown not page-aligned (%d markers, %d pages): ignored", len(marks), pages)
        return {}
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = clean_lines(text[m.end():end])
    return out


# ---------- per book ----------

def extract_book(cfg: Config, con: sqlite3.Connection, row: sqlite3.Row, opt: Options) -> dict:
    sha, ext = row["sha256"], row["ext"]
    src = cfg.sources / row["path"]
    obj = cfg.objects / sha
    manifest_path = obj / "extract.json"
    if manifest_path.is_file() and not opt.force:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        if m.get("extractor_version") == EXTRACTOR_VERSION and not (
                (opt.nougat or opt.llm_ocr) and m.get("low_quality_pages")):
            log.info("skip (cached): %s", row["title"])
            return {"sha256": sha, "skipped": True}

    t = Timer()
    log.info("extract: %s — %s [%s, %s]", row["title"], row["author"], ext, sha[:12])
    if ext == "pdf":
        raw = pdf_pages_text(src, row["pdf_pages"])
    elif ext == "djvu":
        if not (djvu_tool("djvutxt") and djvu_tool("djvused")):
            log.error("  djvutxt not installed: cannot extract %s", row["path"])
            return {"sha256": sha, "error": "djvutxt missing"}
        raw = djvu_pages_text(src)
    elif ext == "epub":
        raw = epub_sections_text(src)
    else:
        return {"sha256": sha, "error": f"unsupported .{ext}"}

    results: dict[int, PageResult] = {}
    for i, text in enumerate(raw, 1):
        lines = clean_lines(text)
        results[i] = PageResult(lines, "textlayer", quality(lines))

    low = [p for p, r in results.items() if r.quality < opt.q_min]
    # Blank pages (no text, no images) are not low quality, they are blank.
    if ext == "pdf":
        for p in list(low):
            if not results[p].lines and not page_has_images(src, p):
                results[p].method = "blank"
                low.remove(p)
    log.info("  %d pages, %d below quality %.2f", len(results), len(low), opt.q_min)

    if low and opt.use_legacy:
        legacy = legacy_pages(cfg, row["seed_markdown"], len(results))
        for p in list(low):
            if p in legacy and quality(legacy[p]) > results[p].quality:
                results[p] = PageResult(legacy[p], "legacy", quality(legacy[p]),
                                        [f"textlayer q={results[p].quality}"])
                if results[p].quality >= opt.q_min:
                    low.remove(p)
    if low and opt.nougat and ext == "pdf":
        if ocr.nougat_available():
            log.info("  nougat: %d pages", len(low))
            for p, (md, state) in ocr.nougat_ocr(src, low).items():
                lines = clean_lines(md)
                q = quality(lines)
                if state == "ok" and q > results[p].quality:
                    results[p] = PageResult(lines, "nougat", q, [f"prev q={results[p].quality}"])
                else:
                    log.debug("  p%d nougat %s q=%.3f: not used", p, state, q)
            low = [p for p in low if results[p].quality < opt.q_min]
        else:
            log.warning("  nougat requested but docker or the model folder (%s) is missing", ocr.nougat_models())
    if low and opt.llm_ocr and ext == "pdf":
        from concurrent.futures import ThreadPoolExecutor
        workers = int(os.environ.get("LIBRARIAN_OCR_WORKERS", "3"))
        log.info("  LLM OCR: %d pages, %d workers (%s)", len(low), workers,
                 os.environ.get("LIBRARIAN_OCR_BACKEND", "codex"))
        from concurrent.futures import as_completed
        ocr_t, done, failed = Timer(), 0, 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(ocr.llm_ocr, src, pg): pg for pg in low}
            for fut in as_completed(futs):
                p = futs[fut]
                done += 1
                try:
                    md, method = fut.result()
                except Exception:  # noqa: BLE001
                    log.exception("  p%d OCR crashed", p)
                    md, method = None, "llm-ocr"
                if md is None:
                    failed += 1
                else:
                    lines = clean_lines(md)
                    results[p] = PageResult(lines, method, quality(lines), [f"prev q={results[p].quality}"])
                    log.debug("  p%d %s q=%.3f", p, method, results[p].quality)
                if done % 10 == 0 or done == len(low):
                    rate = done / max(ocr_t.s, 1e-6) * 60
                    eta = (len(low) - done) / max(rate, 1e-6)
                    log.info("    OCR %d/%d pages, %d failed, %.1f pages/min, ~%.0f min left",
                             done, len(low), failed, rate, eta)
        low = [p for p in low if results[p].quality < opt.q_min]

    pages_dir = obj / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for p, r in results.items():
        (pages_dir / f"{p:04d}.md").write_text("\n".join(r.lines) + ("\n" if r.lines else ""),
                                               encoding="utf-8", newline="\n")
        log.debug("  p%04d %-9s q=%.3f lines=%d %s", p, r.method, r.quality, len(r.lines), " ".join(r.notes))
    methods = {}
    for r in results.values():
        methods[r.method] = methods.get(r.method, 0) + 1
    manifest = {
        "sha256": sha, "path": row["path"], "title": row["title"], "author": row["author"],
        "extractor_version": EXTRACTOR_VERSION, "extracted_at": now(), "pages": len(results),
        "methods": methods, "low_quality_pages": low,
        "page": {str(p): {"method": r.method, "quality": r.quality, "lines": len(r.lines)}
                 for p, r in results.items()},
    }
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    con.execute("DELETE FROM pages WHERE sha256=?", (sha,))
    con.executemany(
        "INSERT INTO pages(sha256, pdf_page, method, quality, line_count) VALUES(?,?,?,?,?)",
        [(sha, p, r.method, r.quality, len(r.lines)) for p, r in results.items()])
    # A handful of low pages (covers, plates, ornaments) is normal; flag only real gaps.
    status = "extracted-partial" if len(low) > max(3, 0.05 * len(results)) else "extracted"
    reason = f"{len(low)} pages below quality {opt.q_min}: {low[:30]}" if low else None
    con.execute("UPDATE books SET status=?, reason=?, updated_at=? WHERE sha256=?", (status, reason, now(), sha))
    con.commit()
    log.info("  done in %.1fs: %s%s", t.s, methods, f"; still low: {low[:20]}{'…' if len(low) > 20 else ''}" if low else "")
    return {"sha256": sha, "pages": len(results), "methods": methods, "low": len(low), "seconds": round(t.s, 1)}
