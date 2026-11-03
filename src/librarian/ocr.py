"""OCR escalation backends: nougat (local GPU, via the legacy docker image) and OpenAI vision.

Claude is deliberately not an OCR backend (see HANDOFF.md).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
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


def openai_ocr(pdf: Path, page: int) -> Optional[str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.warning("OpenAI OCR skipped: OPENAI_API_KEY not set")
        return None
    model = os.environ.get("LIBRARIAN_OCR_MODEL", "gpt-4.1")
    with tempfile.TemporaryDirectory() as td:
        png = render_page(pdf, page, Path(td))
        b64 = base64.b64encode(png.read_bytes()).decode()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0,
    }
    req = urllib.request.Request(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 - log and fall back
        log.error("OpenAI OCR failed for %s p%d: %s", pdf.name, page, e)
        return None
    usage = data.get("usage", {})
    log.debug("openai ocr %s p%d model=%s tokens=%s", pdf.name, page, model, usage)
    text = data["choices"][0]["message"]["content"] or ""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


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
    """Dispatch on $LIBRARIAN_OCR_BACKEND: 'codex' (default) or 'openai' (API key)."""
    backend = os.environ.get("LIBRARIAN_OCR_BACKEND", "codex").lower()
    if backend == "openai":
        return openai_ocr(pdf, page), "llm-ocr:openai"
    return codex_ocr(pdf, page), "llm-ocr:codex"


def nougat_available() -> bool:
    return bool(shutil.which("docker")) and bool(os.environ.get("LIBRARIAN_NOUGAT_MODELS"))


def nougat_ocr(pdf: Path, pages: list[int]) -> dict[int, str]:
    """Run nougat on a set of pages via the legacy image. Returns {page: markdown}."""
    image = os.environ.get("LIBRARIAN_NOUGAT_IMAGE", "lra-nougat-pilot:0.1.17")
    models = os.environ["LIBRARIAN_NOUGAT_MODELS"]  # e.g. D:\Readings\indexes\lra\nougat-models
    model = os.environ.get("LIBRARIAN_NOUGAT_MODEL", "0.1.0-base")
    out: dict[int, str] = {}
    with tempfile.TemporaryDirectory() as td:
        for p in pages:
            work = Path(td) / f"p{p}"
            work.mkdir()
            cmd = ["docker", "run", "--rm", "--gpus", "all", "-v", f"{pdf}:/input/source.pdf:ro",
                   "-v", f"{work}:/output", "-v", f"{models}:/models", image, "run",
                   "--no-skipping", "--model", model, "--pages", str(p), "/input/source.pdf"]
            log.debug("nougat: %s", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            if r.returncode != 0:
                log.error("nougat failed %s p%d: %s", pdf.name, p, r.stderr.decode(errors="replace")[-500:])
                continue
            mmd = sorted(work.rglob("*.mmd"))
            if mmd:
                out[p] = mmd[0].read_text(encoding="utf-8", errors="replace")
    return out
