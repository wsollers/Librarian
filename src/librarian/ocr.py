"""LLM OCR through the Codex CLI (ChatGPT plan). The only OCR backend: the 2026-11 bake-off
(scripts/bakeoff) showed nougat and Tesseract corrupt mathematics and that only Codex keeps the
printed line breaks that citations depend on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .log import log

OCR_PROMPT = (
    "Transcribe this book page exactly. Output Markdown. Keep the page's own line breaks: "
    "one output line per printed line. Write mathematics as LaTeX ($...$ inline, $$...$$ display). "
    "Include running heads and page numbers as they appear. Do not summarise, correct or add anything."
)


def render_page(pdf: Path, page: int, out_dir: Path, dpi: int = 200) -> Path:
    prefix = out_dir / f"p{page:04d}"
    subprocess.run(["pdftoppm", "-q", "-r", str(dpi), "-f", str(page), "-l", str(page), "-png",
                    "-singlefile", str(pdf), str(prefix)], check=True, timeout=120)
    return prefix.with_suffix(".png")


def codex_ocr(pdf: Path, page: int) -> Optional[str]:
    """OCR via the Codex CLI (`codex exec`), signed in with the ChatGPT account: subscription
    usage, no API key. Runs in an empty temp dir with a read-only sandbox."""
    exe = shutil.which(os.environ.get("LIBRARIAN_CODEX", "codex"))
    if not exe:
        log.warning("Codex OCR skipped: codex CLI not on PATH")
        return None
    with tempfile.TemporaryDirectory() as td:
        png = render_page(pdf, page, Path(td))
        out = Path(td) / "out.md"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--image", str(png), "--output-last-message", str(out)]
        model = os.environ.get("LIBRARIAN_CODEX_MODEL")
        if model:
            cmd += ["--model", model]
        cmd.append(OCR_PROMPT)
        try:
            r = subprocess.run(cmd, cwd=td, capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=int(os.environ.get("LIBRARIAN_CODEX_TIMEOUT", "300")))
        except subprocess.TimeoutExpired:
            log.error("Codex OCR timed out: %s p%d", pdf.name, page)
            return None
        if r.returncode != 0 or not out.is_file():
            log.error("Codex OCR failed %s p%d (exit %s): %s", pdf.name, page, r.returncode,
                      r.stderr.decode("utf-8", "replace")[-400:])
            return None
        text = out.read_text(encoding="utf-8", errors="replace")
    log.debug("codex ocr %s p%d chars=%d", pdf.name, page, len(text))
    if text.lstrip().startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


def llm_ocr(pdf: Path, page: int) -> tuple[Optional[str], str]:
    return codex_ocr(pdf, page), "llm-ocr:codex"
