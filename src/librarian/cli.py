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


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
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
    p = sub.add_parser("audit", help="per-book correctness check: reports/audit.md + audit.csv")
    p.add_argument("--mark", action="store_true", help="queue damaged pages for the next extract --llm-ocr")
    p.add_argument("--no-fonts", action="store_true", help="skip the pdffonts evidence check")

    def add_sel(p):
        p.add_argument("--select", default="priority", help="'priority' (config/priority.txt), '@file' (one pattern per line), 'all', or a path substring / sha prefix")
        p.add_argument("--limit", type=int, help="process at most N books")
        p.add_argument("--force", action="store_true", help="redo even if cached")

    def add_extract_opts(p):
        p.add_argument("--llm-ocr", action="store_true", help="OCR low-quality pages with the Codex CLI (ChatGPT plan)")
        p.add_argument("--body-only", action="store_true", help="with --llm-ocr: skip the first/last 5%% of each book (covers, front/back matter)")
        p.add_argument("--no-legacy", action="store_true", help="do not reuse legacy lra extraction")

    def add_embed(p):
        p.add_argument("--embed", default=None, help="embedder spec, e.g. st:BAAI/bge-base-en-v1.5, ollama:nomic-embed-text")

    p = sub.add_parser("extract", help="per-page Markdown with escalation"); add_sel(p); add_extract_opts(p)
    p = sub.add_parser("pagemap", help="printed page numbers (detection + pagemaps/ overrides), chapters and sections"); add_sel(p)
    p = sub.add_parser("index", help="FTS5 + embeddings"); add_sel(p); add_embed(p)
    p.add_argument("--reembed", action="store_true", help="drop all vectors (embedder changed)")
    p = sub.add_parser("run", help="inventory, extract, pagemap, index for a selection")
    add_sel(p); add_extract_opts(p); add_embed(p)
    p = sub.add_parser("djvu2pdf", help="DjVu -> PDF with the DjVu hidden text as a searchable text layer")
    p.add_argument("djvu", help="DjVu file (absolute, or relative to the sources root)")
    p.add_argument("--replace", help="existing PDF (same book) to retire to _duplicates; the new PDF takes its path")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--keep-djvu", action="store_true", help="leave the DjVu in Sources (default: retire it too)")
    p = sub.add_parser("leancheck", help="OCR confidence check: statements -> Lean 4 + Mathlib (sorry) -> compile + back-translation")
    p.add_argument("stage", choices=["section", "statements", "formalize", "backcheck", "compile", "report"],
                   help="section: Codex + page images + Lean tools per chunk (recommended); the others are the older text-only pipeline")
    p.add_argument("name", help="work dir name under <data>/leancheck/")
    p.add_argument("--select", help="statements: the book (substring, as for run --select)")
    p.add_argument("--pages", help="statements: PDF page range A-B")
    p.add_argument("--mmd", help="statements: read .mmd chunks (pages-A-B.mmd, e.g. old nougat output) from this dir instead of pages/")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--project", help="compile: Lean project with Mathlib (default $LIBRARIAN_LEAN_PROJECT)")
    p.add_argument("--no-plausible", action="store_true")
    p.add_argument("--chunk-pages", type=int, default=8, help="section: at most N pages per Codex session")
    p.add_argument("--force", action="store_true", help="section: redo chunks that already have a result")
    p = sub.add_parser("search", help="hybrid search")
    p.add_argument("query")
    p.add_argument("--mode", choices=["hybrid", "phrase", "fts", "semantic"], default="hybrid")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--book", help="restrict to books whose title/author/path contains this")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timing", action="store_true", help="print time per phase (stderr)")
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
    elif a.cmd == "djvu2pdf":
        summary.update(djvu_to_pdf(cfg, a))
    elif a.cmd == "audit":
        from .audit import audit
        summary.update(audit(cfg, connect(cfg.db_path), mark=a.mark, fonts=not a.no_fonts))
    elif a.cmd == "status":
        con = connect(cfg.db_path)
        print(json.dumps({
            "books_by_status": dict(con.execute("SELECT status, count(*) FROM books GROUP BY status").fetchall()),
            "pages_by_method": dict(con.execute("SELECT coalesce(method,'?'), count(*) FROM pages GROUP BY method").fetchall()),
            "meta": dict(con.execute("SELECT key, value FROM meta").fetchall()),
        }, indent=2))
        return 0
    elif a.cmd == "leancheck":
        summary.update(lean_check(cfg, a))
    elif a.cmd in ("extract", "pagemap", "index", "run"):
        summary.update(run_stages(cfg, a))
    elif a.cmd == "search":
        import time as _t
        t_start = _t.perf_counter()
        from . import embed, search
        from .index import connect_index
        con = connect(cfg.db_path)
        connect_index(con)
        spec = a.embed or (con.execute("SELECT value FROM meta WHERE key='embed_spec'").fetchone() or [None])[0]
        emb = embed.load(spec) if spec and a.mode in ("hybrid", "semantic") else None
        rr = embed.load_reranker(a.rerank) if a.mode in ("hybrid", "semantic") else None
        ex = embed.load_expander(a.expand) if a.mode in ("hybrid", "semantic") else None
        t_models = _t.perf_counter() - t_start
        hits = search.search(cfg, con, a.query, emb, a.mode, a.k, a.book, rr, a.explain, ex)
        if a.json:
            print(json.dumps([search.as_dict(h) for h in hits], indent=2, ensure_ascii=False))
        else:
            for i, h in enumerate(hits, 1):
                print(f"{i:2d}. {search.format_hit(h)}")
        if a.timing:
            print(f"# timing: load models {t_models:.1f}s | " +
                  " | ".join(f"{k} {v:.1f}s" for k, v in search.LAST_TIMING.items()), file=sys.stderr)
        return 0

    summary["seconds"] = round(t.s, 1)
    if log_path:
        write_summary(log_path, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "books"}, indent=2, default=str))
    return 0


def _retire(cfg, rel: Path, reason: str, replacement: str) -> None:
    """Move a file under Sources to <sources parent>/_duplicates/Sources/<same path>; log it."""
    import datetime
    root = cfg.sources.parent / "_duplicates"
    dst = root / "Sources" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(dst.stem + " (2)" + dst.suffix)
    (cfg.sources / rel).replace(dst)
    with (root / "MOVED.tsv").open("a", encoding="utf-8") as f:
        f.write(f"{rel.as_posix()}\t{replacement}\t{reason} ({datetime.date.today()})\n")
    log.info("retired %s -> %s", rel, dst)


def djvu_to_pdf(cfg, a) -> dict:
    from .djvu2pdf import convert
    djvu = Path(a.djvu)
    if not djvu.is_absolute():
        djvu = cfg.sources / djvu
    if not djvu.is_file():
        raise SystemExit(f"not found: {djvu}")
    out = djvu.with_suffix(".pdf")
    if a.replace:
        old = Path(a.replace)
        old = old if old.is_absolute() else cfg.sources / old
        out = old
    tmp = out.with_name(out.stem + ".from-djvu.pdf")
    res = convert(djvu, tmp, dpi=a.dpi, title=out.stem)
    if out.exists():
        _retire(cfg, out.relative_to(cfg.sources), "damaged text layer; rebuilt from DjVu", djvu.name)
    tmp.replace(out)
    if not a.keep_djvu:
        _retire(cfg, djvu.relative_to(cfg.sources), "converted to PDF with its text layer", out.name)
    res["pdf"] = str(out)
    log.info("next: scripts\\run.ps1 run --select \"%s\"", out.stem)
    return res


def _page_range(spec: str | None) -> tuple[int, int]:
    if not spec:
        return 1, 10**6
    a_, _, b_ = spec.partition("-")
    return int(a_), int(b_ or a_)


def lean_check(cfg, a) -> dict:
    import re as _re
    from . import leancheck as lc
    work = cfg.data / "leancheck" / a.name
    work.mkdir(parents=True, exist_ok=True)
    if a.stage == "section" or (a.stage == "report" and any(work.glob("c*/result.json"))):
        from . import leansection
        if a.stage == "report":
            return leansection.report(work)
        if not (a.select and a.pages):
            raise SystemExit("leancheck section needs --select <book> and --pages A-B")
        con = connect(cfg.db_path)
        books = select_books(con, a.select)
        if len(books) != 1:
            raise SystemExit(f"--select matched {len(books)} books; make it specific: "
                             + "; ".join(b["title"] for b in books[:8]))
        lo, hi = _page_range(a.pages)
        return leansection.run(cfg, con, books[0], lo, hi, a.name, max_pages=a.chunk_pages, force=a.force)
    if a.stage == "statements":
        lo, hi = _page_range(a.pages)
        pages: dict[int, list[str]] = {}
        book = a.name
        if a.mmd:
            # nougat chunks have no page breaks: key each chunk by its first PDF page
            for f in sorted(Path(a.mmd).glob("pages-*.mmd")):
                m = _re.match(r"pages-(\d+)-(\d+)", f.stem)
                if m and int(m.group(2)) >= lo and int(m.group(1)) <= hi:
                    pages[int(m.group(1))] = f.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            if not a.select:
                raise SystemExit("leancheck statements needs --select <book> (or --mmd <dir>)")
            con = connect(cfg.db_path)
            books = select_books(con, a.select)
            if len(books) != 1:
                raise SystemExit(f"--select matched {len(books)} books; make it specific: "
                                 + "; ".join(b["title"] for b in books[:8]))
            b = books[0]
            book = f'{b["title"]} - {b["author"] or "Unknown"}'
            for f in sorted((cfg.objects / b["sha256"] / "pages").glob("*.md")):
                n = int(f.stem)
                if lo <= n <= hi:
                    pages[n] = f.read_text(encoding="utf-8").splitlines()
        rows = lc.extract(pages, book)
        lc._write(work / "statements.jsonl", rows)
        kinds: dict[str, int] = {}
        for r in rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        log.info("leancheck %s: %d statements from %d pages -> %s", a.name, len(rows), len(pages), work)
        return {"work": str(work), "pages": len(pages), "statements": len(rows), "kinds": kinds}
    if a.stage == "formalize":
        lc.formalize(work, a.workers)
    elif a.stage == "backcheck":
        lc.backcheck(work, a.workers)
    elif a.stage == "compile":
        proj = a.project or os.environ.get("LIBRARIAN_LEAN_PROJECT")
        if not proj:
            raise SystemExit("leancheck compile needs --project <lean project with Mathlib>")
        lc.compile_all(work, Path(proj), plausible=not a.no_plausible)
    elif a.stage == "report":
        return {"work": str(work), **lc.report(work)}
    return {"work": str(work), "stage": a.stage}


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
        opt = ex.Options(force=a.force, use_legacy=not a.no_legacy, llm_ocr=a.llm_ocr,
                         body_only=a.body_only)
        for i, b in enumerate(books, 1):
            log.info("[%d/%d]", i, len(books))
            try:
                out["books"].append(ex.extract_book(cfg, con, b, opt))
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("extract failed: %s", b["path"])
    if "pagemap" in stages:
        from . import sections
        for b in books:
            try:
                pagemap.map_book(cfg, con, b["sha256"], b["title"])
                sections.build(cfg, con, b["sha256"], b["title"], b["path"])
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("pagemap/sections failed: %s", b["path"])
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
