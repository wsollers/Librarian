"""OCR bake-off runner (hal5000). Runs each engine over SET/<level>/pNNNN.jpg and writes
OUT/<engine>/<level>/pNNNN.md plus timing.json {seconds, pages}. Resumable: an engine/level
with timing.json is skipped unless --force.

Only Codex remains (the production OCR). The 2026-11-04 bake-off also ran Tesseract, nougat
0.1.0-small/base and qwen2.5vl:7b; results and outputs are in D:\\Readings\\librarian\\bakeoff\\out.
Re-run after changing the OCR prompt or Codex model and compare with score.py.

usage (from F:\\repos\\librarian, inside the .venv):
  python scripts\\bakeoff\\run_engines.py SET OUT codex
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from librarian import ocr  # noqa: E402

PROMPT = ocr.OCR_PROMPT
LEVELS = ("scan", "bad")


def images(set_dir: Path, level: str) -> list[Path]:
    return sorted((set_dir / level).glob("p*.jpg"))


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


ENGINES = {
    "codex": lambda imgs, d: run_per_image(codex_one, imgs, d, 3),
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
