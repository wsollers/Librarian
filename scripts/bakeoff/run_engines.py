"""OCR bake-off runner (hal5000). Runs each engine over SET/<level>/pNNNN.jpg and writes
OUT/<engine>/<level>/pNNNN.md plus timing.json {seconds, pages}. Resumable: an engine/level
with timing.json is skipped unless --force.

Engines
  codex          Codex CLI (ChatGPT plan), same prompt as librarian --llm-ocr     [librarian .venv]
  qwen25vl-7b    Ollama qwen2.5vl:7b, same prompt                                 [Ollama running]
  nougat-small   nougat 0.1.0-small in the lra-nougat-pilot docker image          [docker]
  nougat-base    nougat 0.1.0-base (weights download into nougat-models on first run)
  marker         datalab marker (Surya layout/OCR + texify math), --force_ocr     [BAKEOFF_MARKER=path to marker exe]
  olmocr         olmOCR 2 7B FP8 via alleninstituteforai/olmocr:latest-with-model (~30 GB image) [docker]

usage (from F:\\repos\\librarian, inside the .venv):
  python scripts\\bakeoff\\run_engines.py SET OUT codex qwen25vl-7b nougat-small nougat-base [marker] [olmocr]
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from librarian import ocr  # noqa: E402

PROMPT = ocr.OCR_PROMPT
LEVELS = ("scan", "bad")


def images(set_dir: Path, level: str) -> list[Path]:
    return sorted((set_dir / level).glob("p*.jpg"))


def page_pdfs(imgs: list[Path], td: Path) -> list[Path]:
    """One single-page PDF per image (engines that take PDFs). 200 dpi nominal."""
    from PIL import Image
    out = []
    for f in imgs:
        p = td / (f.stem + ".pdf")
        Image.open(f).convert("RGB").save(p, resolution=200)
        out.append(p)
    return out


def strip_fence(t: str) -> str:
    s = t.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s


# ---------------------------------------------------------------- engines: image -> text
def codex_one(img: Path) -> str:
    exe = shutil.which(os.environ.get("LIBRARIAN_CODEX", "codex"))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.md"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--image", str(img), "--output-last-message", str(out), PROMPT]
        subprocess.run(cmd, cwd=td, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
        return strip_fence(out.read_text(encoding="utf-8", errors="replace")) if out.is_file() else ""


def qwen_one(img: Path, model: str = "qwen2.5vl:7b") -> str:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not host.startswith("http"):
        host = "http://" + host
    body = {"model": model, "stream": False, "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
            "messages": [{"role": "user", "content": PROMPT,
                          "images": [base64.b64encode(img.read_bytes()).decode()]}]}
    req = urllib.request.Request(host + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return strip_fence(json.loads(r.read())["message"]["content"])


def run_per_image(fn, imgs, dest: Path, workers: int) -> None:
    def one(f):
        t = time.perf_counter()
        try:
            txt = fn(f)
        except Exception as e:  # keep going; an empty page scores as a failure
            print(f"    {f.name}: {e}", flush=True)
            txt = ""
        (dest / (f.stem + ".md")).write_text(txt, encoding="utf-8")
        print(f"    {f.stem} {len(txt)} chars {time.perf_counter() - t:.0f}s", flush=True)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, imgs))


# ---------------------------------------------------------------- engines: pdf batch
def nougat(imgs, dest: Path, model: str) -> None:
    os.environ["LIBRARIAN_NOUGAT_MODEL"] = model
    with tempfile.TemporaryDirectory() as td:
        from PIL import Image
        pdf = Path(td) / "level.pdf"
        ims = [Image.open(f).convert("RGB") for f in imgs]
        ims[0].save(pdf, save_all=True, append_images=ims[1:], resolution=200)
        res = ocr.nougat_ocr(pdf, list(range(1, len(imgs) + 1)))
    for i, f in enumerate(imgs, 1):
        md, state = res.get(i, ("", "missing"))
        (dest / (f.stem + ".md")).write_text(md, encoding="utf-8")
        print(f"    {f.stem} {state} {len(md)} chars", flush=True)


def marker(imgs, dest: Path) -> None:
    exe = os.environ.get("BAKEOFF_MARKER") or shutil.which("marker")
    if not exe:
        raise SystemExit("marker: set BAKEOFF_MARKER to the marker executable of the bake-off venv")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "in").mkdir()
        page_pdfs(imgs, td / "in")
        subprocess.run([exe, str(td / "in"), "--output_dir", str(td / "out"), "--output_format", "markdown",
                        "--force_ocr", "--disable_image_extraction"], check=True)
        for f in imgs:
            hits = list((td / "out").rglob(f.stem + ".md"))
            (dest / (f.stem + ".md")).write_text(hits[0].read_text(encoding="utf-8") if hits else "", encoding="utf-8")


def olmocr(imgs, dest: Path) -> None:
    image = os.environ.get("BAKEOFF_OLMOCR_IMAGE", "alleninstituteforai/olmocr:latest-with-model")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "in").mkdir()
        page_pdfs(imgs, td / "in")
        cmd = ["docker", "run", "--rm", "--gpus", "all", "-v", f"{td}:/workspace", image, "-c",
               "olmocr /workspace/ws --markdown --pdfs /workspace/in/*.pdf"]
        subprocess.run(cmd, check=True)
        for f in imgs:
            hits = list((td / "ws").rglob(f.stem + ".md"))
            (dest / (f.stem + ".md")).write_text(hits[0].read_text(encoding="utf-8") if hits else "", encoding="utf-8")


ENGINES = {
    "codex": lambda imgs, d: run_per_image(codex_one, imgs, d, 3),
    "qwen25vl-7b": lambda imgs, d: run_per_image(qwen_one, imgs, d, 1),
    "nougat-small": lambda imgs, d: nougat(imgs, d, "0.1.0-small"),
    "nougat-base": lambda imgs, d: nougat(imgs, d, "0.1.0-base"),
    "marker": marker,
    "olmocr": olmocr,
}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    set_dir, out, engines = Path(args[0]), Path(args[1]), args[2:]
    for e in engines:
        if e not in ENGINES:
            print(f"unknown engine {e}; choose from {', '.join(ENGINES)}")
            return 2
    for e in engines:
        for lvl in LEVELS:
            dest = out / e / lvl
            if (dest / "timing.json").is_file() and not force:
                print(f"{e}/{lvl}: done, skipped")
                continue
            dest.mkdir(parents=True, exist_ok=True)
            imgs = images(set_dir, lvl)
            print(f"{e}/{lvl}: {len(imgs)} pages", flush=True)
            t = time.perf_counter()
            try:
                ENGINES[e](imgs, dest)
            except Exception as ex:
                print(f"{e}/{lvl} FAILED: {ex}", flush=True)
                continue
            s = time.perf_counter() - t
            (dest / "timing.json").write_text(json.dumps({"seconds": round(s, 1), "pages": len(imgs)}))
            print(f"{e}/{lvl}: {s:.0f}s ({s / len(imgs):.1f} s/page)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
