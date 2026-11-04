"""Build the OCR bake-off set from a born-digital PDF.

For each PDF page: ground truth text (pdftotext) and two degraded "scans":
  scan  - 200 dpi grey, 0.6 deg skew, paper texture + sensor noise, JPEG q60
  bad   - 150 dpi, 1.2 deg skew, blur, uneven lighting, heavy noise, hard binarisation, JPEG q45
Deterministic (seeded per page), so every engine sees identical images.

usage: python make_pages.py book.pdf OUTDIR 71-74,86-90
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

LEVELS = {
    "scan": dict(dpi=200, skew=0.6, blur=0.0, light=0.08, noise=10, binar=False, q=60),
    "bad": dict(dpi=150, skew=1.2, blur=0.9, light=0.22, noise=22, binar=True, q=45),
}


def pages(spec):
    for part in spec.split(","):
        a, _, b = part.partition("-")
        yield from range(int(a), int(b or a) + 1)


def render(pdf, p, dpi, out):
    subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", str(dpi), "-gray", "-singlefile",
                    "-png", str(pdf), str(out)], check=True)
    return Image.open(str(out) + ".png").convert("L")


def degrade(img, cfg, seed):
    rng = np.random.default_rng(seed)
    img = img.rotate(cfg["skew"] * (1 if seed % 2 else -1), resample=Image.BICUBIC, expand=False, fillcolor=255)
    if cfg["blur"]:
        img = img.filter(ImageFilter.GaussianBlur(cfg["blur"]))
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape
    # uneven lighting: a diagonal gradient plus a soft blotch
    yy, xx = np.mgrid[0:h, 0:w]
    grad = 1 - cfg["light"] * (xx / w * 0.6 + yy / h * 0.4)
    cy, cx = rng.uniform(0.2, 0.8) * h, rng.uniform(0.2, 0.8) * w
    blotch = 1 - cfg["light"] * 0.7 * np.exp(-(((yy - cy) / (0.3 * h)) ** 2 + ((xx - cx) / (0.3 * w)) ** 2))
    a = a * grad * blotch
    a += rng.normal(0, cfg["noise"], a.shape)
    # salt/pepper specks
    specks = rng.random(a.shape)
    a[specks < 0.0008] = 30
    a[specks > 0.9995] = 255
    a = np.clip(a, 0, 255)
    if cfg["binar"]:
        a = np.where(a > 150, 255, 0)
    img = Image.fromarray(a.astype(np.uint8))
    return img


def main():
    pdf, out, spec = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    (out / "truth").mkdir(parents=True, exist_ok=True)
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)
    for p in pages(spec):
        txt = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), "-layout", str(pdf), "-"],
                             capture_output=True, text=True, check=True).stdout
        (out / "truth" / f"p{p:04d}.txt").write_text(txt, encoding="utf-8")
        for lvl, cfg in LEVELS.items():
            d = out / lvl
            d.mkdir(exist_ok=True)
            img = degrade(render(pdf, p, cfg["dpi"], tmp / f"p{p}-{lvl}"), cfg, seed=p * 7 + len(lvl))
            img.save(d / f"p{p:04d}.jpg", quality=cfg["q"])
        print("page", p)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main()
