"""Run logging: concise console, detailed file log per run under <data>/logs/."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("librarian")


def setup(data_dir: Path, command: str, verbose: bool = False) -> Path:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = logs / f"{stamp}-{command}.log"

    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(fh)
    log.addHandler(ch)
    log.info("log file: %s", path)
    log.debug("argv=%s python=%s prefix=%s", sys.argv, sys.version.split()[0], sys.prefix)
    return path


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    @property
    def s(self) -> float:
        return time.perf_counter() - self.t0


def write_summary(log_path: Path, summary: dict) -> None:
    out = log_path.with_suffix(".summary.json")
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("summary: %s", out)
