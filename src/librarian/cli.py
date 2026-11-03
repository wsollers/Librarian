"""librarian CLI. Each stage is incremental and safe to rerun."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__, config, inventory


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="librarian", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", type=Path, help="TOML config (default: config/local.toml)")
    ap.add_argument("--sources", help="sources root (overrides config)")
    ap.add_argument("--data", help="data directory for index, objects, reports")
    ap.add_argument("--legacy", help="legacy lra index root to seed from")
    ap.add_argument("--manifest", help="legacy source-manifest.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory", help="phase 1: hash, dedupe, probe; write coverage report")
    inv.add_argument("--rehash", action="store_true", help="ignore cached hashes and probes")
    inv.add_argument("-q", "--quiet", action="store_true")

    sub.add_parser("report", help="rewrite the coverage report from the current index")

    for name, phase in (("extract", 2), ("index", 2), ("search", 2), ("publish", 3)):
        sub.add_parser(name, help=f"not implemented yet (phase {phase})")

    a = ap.parse_args(argv)
    cfg = config.load(a.config, sources=a.sources, data=a.data, legacy=a.legacy, manifest=a.manifest)

    if a.cmd == "inventory":
        print(json.dumps(inventory.run(cfg, rehash=a.rehash, verbose=not a.quiet), indent=2))
    elif a.cmd == "report":
        from .db import connect
        con = connect(cfg.db_path)
        print(json.dumps(inventory.report(con, cfg), indent=2))
    else:
        ap.error(f"'{a.cmd}' is not implemented yet; see HANDOFF.md phases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
