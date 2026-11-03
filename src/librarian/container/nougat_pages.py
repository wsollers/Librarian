"""Runs INSIDE the lra-nougat-pilot image (nougat-ocr 0.1.17). One model load, many pages,
one output file per page: /output/pNNNN.mmd, plus /output/status.json.

usage: python nougat_pages.py --pdf /input/source.pdf --pages 1,2,5-9 --out /output [--model 0.1.0-small]
"""
import argparse
import json
import sys
import time
from functools import partial
from pathlib import Path


def parse_pages(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", required=True, help="1-based")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="0.1.0-small")
    ap.add_argument("--batchsize", type=int, default=4)
    a = ap.parse_args()

    import torch
    from nougat import NougatModel
    from nougat.postprocessing import markdown_compatible
    from nougat.utils.checkpoint import get_checkpoint
    from nougat.utils.dataset import LazyDataset
    from nougat.utils.device import move_to_device

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pages = parse_pages(a.pages)
    t0 = time.time()
    model = NougatModel.from_pretrained(get_checkpoint(None, model_tag=a.model))
    model = move_to_device(model, bf16=True, cuda=torch.cuda.is_available())
    model.eval()
    load_s = time.time() - t0

    ds = LazyDataset(Path(a.pdf), partial(model.encoder.prepare_input, random_padding=False),
                     [p - 1 for p in pages])
    dl = torch.utils.data.DataLoader(torch.utils.data.ConcatDataset([ds]), batch_size=a.batchsize,
                                     shuffle=False, collate_fn=LazyDataset.ignore_none_collate)
    status = {"model": a.model, "cuda": torch.cuda.is_available(), "load_seconds": round(load_s, 1), "pages": {}}
    k = 0
    for sample, _ in dl:
        if sample is None:
            continue
        res = model.inference(image_tensors=sample, early_stopping=True)
        preds = res["predictions"]
        repeats = res.get("repeats") or [None] * len(preds)
        for j, text in enumerate(preds):
            p = pages[k]
            k += 1
            rep = repeats[j] if j < len(repeats) else None
            if rep is not None and rep > 0:
                state = "repetition"      # output truncated: not trustworthy
            elif rep is not None:
                state = "out-of-domain"   # nougat gave up (cover, figure, odd layout)
            else:
                state = "ok"
            md = markdown_compatible(text) if state == "ok" else ""
            (out / f"p{p:04d}.mmd").write_text(md, encoding="utf-8")
            status["pages"][str(p)] = state
        print(f"nougat {k}/{len(pages)} pages", file=sys.stderr, flush=True)
    status["seconds"] = round(time.time() - t0, 1)
    (out / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
