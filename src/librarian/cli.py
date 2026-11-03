"""librarian: page-exact extraction and hybrid search. Every stage is incremental and logged."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__, config, inventory
from .db import connect
from .log import Timer, log, setup, write_summary

REPO = Path(__file__).resolve().parents[2]


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def select_books(con: sqlite3.Connection, sel: str) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM books WHERE status != 'unsupported' ORDER BY path").fetchall()
    if sel == "all":
        return rows
    if sel == "priority" or sel.startswith("@"):
        path = REPO / "config" / "priority.txt" if sel == "priority" else Path(sel[1:])
        if not path.is_absolute() and not path.is_file():
            path = REPO / path
        pats = [ln.split("#", 1)[0].strip().lower() for ln in path.read_text(encoding="utf-8").splitlines()]
        pats = [p for p in pats if p]
    else:
        pats = [sel.lower()]
    def first(r):  # list order = processing order
        return next((i for i, p in enumerate(pats) if p in r["path"].lower() or r["sha256"].startswith(p)), None)
    return sorted((r for r in rows if first(r) is not None), key=lambda r: (first(r), r["path"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="librarian", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", type=Path, help="TOML config (default: config/local.toml)")
    ap.add_argument("--sources")
    ap.add_argument("--data")
    ap.add_argument("--legacy")
    ap.add_argument("--manifest")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug output on the console too")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inventory", help="phase 1: hash, dedupe, probe; coverage report").add_argument(
        "--rehash", action="store_true")
    sub.add_parser("report", help="rewrite the coverage report")
    sub.add_parser("status", help="counts by status, index stats")

    def add_sel(p):
        p.add_argument("--select", default="priority", help="'priority' (config/priority.txt), '@file' (one pattern per line), 'all', or a path substring / sha prefix")
        p.add_argument("--limit", type=int, help="process at most N books")
        p.add_argument("--force", action="store_true", help="redo even if cached")

    def add_extract_opts(p):
        p.add_argument("--nougat", action="store_true", help="escalate low-quality pages to nougat (docker, GPU)")
        p.add_argument("--llm-ocr", action="store_true", help="escalate still-bad pages to OpenAI vision (OPENAI_API_KEY)")
        p.add_argument("--no-legacy", action="store_true", help="do not reuse legacy lra extraction")

    def add_embed(p):
        p.add_argument("--embed", default=None, help="embedder spec, e.g. st:BAAI/bge-base-en-v1.5, ollama:nomic-embed-text")

    p = sub.add_parser("extract", help="per-page Markdown with escalation"); add_sel(p); add_extract_opts(p)
    p = sub.add_parser("pagemap", help="printed page numbers (detection + pagemaps/ overrides)"); add_sel(p)
    p = sub.add_parser("index", help="FTS5 + embeddings"); add_sel(p); add_embed(p)
    p.add_argument("--reembed", action="store_true", help="drop all vectors (embedder changed)")
    p = sub.add_parser("run", help="inventory, extract, pagemap, index for a selection")
    add_sel(p); add_extract_opts(p); add_embed(p)
    p = sub.add_parser("search", help="hybrid search")
    p.add_argument("query")
    p.add_argument("--mode", choices=["hybrid", "phrase", "fts", "semantic"], default="hybrid")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--book", help="restrict to books whose title/author/path contains this")
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", type=int, default=0, metavar="N", help="print the top N candidates before reranking (stderr)")
    p.add_argument("--expand", default=None, help="query expansion for descriptive queries: 'ollama[:model]' (default, $LIBRARIAN_EXPAND_MODEL or llama3.1:8b) or 'none'")
    p.add_argument("--rerank", default=None, help="cross-encoder model, or 'none' (default $LIBRARIAN_RERANK or BAAI/bge-reranker-v2-m3)")
    add_embed(p)

    a = ap.parse_args(argv)
    if not in_venv() and not os.environ.get("LIBRARIAN_ALLOW_SYSTEM"):
        print("librarian must run inside the project .venv (scripts\\bootstrap.ps1, then scripts\\run.ps1).\n"
              "Set LIBRARIAN_ALLOW_SYSTEM=1 to override.", file=sys.stderr)
        return 2
    cfg = config.load(a.config, sources=a.sources, data=a.data, legacy=a.legacy, manifest=a.manifest)
    log_path = setup(cfg.data, a.cmd, a.verbose) if a.cmd not in ("search", "status") else None
    if log_path:
        log.info("librarian %s %s | sources=%s data=%s legacy=%s", __version__, a.cmd, cfg.sources, cfg.data, cfg.legacy)
    t = Timer()
    summary: dict = {"command": a.cmd, "version": __version__}

    if a.cmd == "inventory":
        summary.update(inventory.run(cfg, rehash=a.rehash, verbose=True))
    elif a.cmd == "report":
        summary.update(inventory.report(connect(cfg.db_path), cfg))
    elif a.cmd == "status":
        con = connect(cfg.db_path)
        print(json.dumps({
            "books_by_status": dict(con.execute("SELECT status, count(*) FROM books GROUP BY status").fetchall()),
            "pages_by_method": dict(con.execute("SELECT coalesce(method,'?'), count(*) FROM pages GROUP BY method").fetchall()),
            "meta": dict(con.execute("SELECT key, value FROM meta").fetchall()),
        }, indent=2))
        return 0
    elif a.cmd in ("extract", "pagemap", "index", "run"):
        summary.update(run_stages(cfg, a))
    elif a.cmd == "search":
        from . import embed, search
        from .index import connect_index
        con = connect(cfg.db_path)
        connect_index(con)
        spec = a.embed or (con.execute("SELECT value FROM meta WHERE key='embed_spec'").fetchone() or [None])[0]
        emb = embed.load(spec) if spec and a.mode in ("hybrid", "semantic") else None
        rr = embed.load_reranker(a.rerank) if a.mode in ("hybrid", "semantic") else None
        ex = embed.load_expander(a.expand) if a.mode in ("hybrid", "semantic") else None
        hits = search.search(cfg, con, a.query, emb, a.mode, a.k, a.book, rr, a.explain, ex)
        if a.json:
            print(json.dumps([search.as_dict(h) for h in hits], indent=2, ensure_ascii=False))
        else:
            for i, h in enumerate(hits, 1):
                print(f"{i:2d}. {search.format_hit(h)}")
        return 0

    summary["seconds"] = round(t.s, 1)
    if log_path:
        write_summary(log_path, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "books"}, indent=2, default=str))
    return 0


def run_stages(cfg, a) -> dict:
    from . import extract as ex, pagemap
    from .index import VectorStore, connect_index, index_book

    stages = {"extract": ["extract"], "pagemap": ["pagemap"], "index": ["index"],
              "run": ["inventory", "extract", "pagemap", "index"]}[a.cmd]
    out: dict = {"stages": stages, "books": []}
    if "inventory" in stages:
        out["inventory"] = inventory.run(cfg, verbose=True)
    con = connect(cfg.db_path)
    books = select_books(con, a.select)[: a.limit]
    log.info("selected %d books (%s)", len(books), a.select)
    for b in books:
        log.debug("  %s | %s | %s", b["sha256"][:12], b["status"], b["path"])

    errors = 0
    if "extract" in stages:
        opt = ex.Options(force=a.force, use_legacy=not a.no_legacy, nougat=a.nougat, llm_ocr=a.llm_ocr)
        for i, b in enumerate(books, 1):
            log.info("[%d/%d]", i, len(books))
            try:
                out["books"].append(ex.extract_book(cfg, con, b, opt))
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("extract failed: %s", b["path"])
    if "pagemap" in stages:
        for b in books:
            try:
                pagemap.map_book(cfg, con, b["sha256"], b["title"])
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("pagemap failed: %s", b["path"])
    if "index" in stages:
        from . import embed
        connect_index(con)
        if getattr(a, "reembed", False):
            for t in ("chunks_vec", "chunks_blob"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            con.execute("DELETE FROM index_state")
            con.execute("DELETE FROM meta WHERE key LIKE 'embed_%'")
        spec = a.embed or (con.execute("SELECT value FROM meta WHERE key='embed_spec'").fetchone() or [embed.DEFAULT])[0]
        emb = embed.load(spec)
        store = VectorStore(con, emb.dim, emb.spec)
        con.commit()
        for i, b in enumerate(books, 1):
            log.info("[%d/%d]", i, len(books))
            try:
                index_book(cfg, con, b["sha256"], b["title"], emb, store, a.force)
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("index failed: %s", b["path"])
    out["selected"] = len(books)
    out["errors"] = errors
    out["status"] = dict(con.execute("SELECT status, count(*) FROM books GROUP BY status").fetchall())
    return out


if __name__ == "__main__":
    raise SystemExit(main())
