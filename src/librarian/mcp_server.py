"""librarian MCP server (stdio), started and stopped by the Claude desktop app.

Lifecycle
* start: answers `initialize` at once; a background thread opens the index read-only and loads
  the embedder, the cross-encoder and the spelling word list (a tool call waits for it).
* Ollama: untouched unless a search asks for expand=true (see ollama.OllamaManager).
* stop (stdin closed by the app): unload the expansion model; stop Ollama if we started it.

stdout is the protocol channel: everything else (print, progress bars, C libraries) is pointed
at stderr before any model library is imported.
"""

from __future__ import annotations

import atexit
import io
import os
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]

INSTRUCTIONS = """Search William's mathematics library (~900 books, page-exact text with printed pages,
chapters and sections). Workflow: search -> read_page on the best hits -> cite.
* Rephrase descriptive questions into the terms a textbook would use and search with them (or run
  a few searches); expand=true asks a slower local model to do this and is rarely needed.
* Narrow with author / book / chapter / section when the question names them, and with subject
  ("Real Analysis", "Metric Spaces, Topology") to keep a result in its field; subjects lists them.
* Never state what a book says from a snippet alone: read_page the cited lines first.
* Cite as: Title — Author, chapter › section, p. <printed> (PDF <pdf>), lines a–b, with the pdf link.
* Right after start-up the models take about a minute to load. A tool called before then answers
  {"loading": true, ...} at once: call status (instant) until ready is true, then retry."""

# How long a tool call waits for start-up before answering "still loading". It must stay well under
# the client's request timeout: a timed-out call makes some clients restart the server, which then
# has to load from scratch again.
LOAD_WAIT_S = float(os.environ.get("LIBRARIAN_MCP_LOAD_WAIT", "15"))


def load_env(path: Path) -> None:
    """The .env that scripts/run.ps1 loads; values already in the environment win."""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.strip() and v:
                os.environ.setdefault(k.strip(), v)
    for k, v in (("PYTHONIOENCODING", "utf-8"), ("HF_HUB_OFFLINE", "1"), ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
                 ("HF_HUB_VERBOSITY", "error"), ("TRANSFORMERS_VERBOSITY", "error"),
                 ("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1"), ("TQDM_DISABLE", "1")):
        os.environ.setdefault(k, v)


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.con: Optional[sqlite3.Connection] = None
        self.emb = self.rr = self.speller = None
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.error: Optional[str] = None
        self.warnings: list[str] = []
        self.loaded_in: Optional[float] = None
        self.started = time.perf_counter()
        self.device = "?"
        self.pages = None                         # search.PageText: page lines from SQLite, not files
        self.vectors = None                       # memvec.MemIndex once loaded; until then SQLite's vectors
        self.vectors_state = "waiting for start-up"
        from .ollama import OllamaManager
        self.ollama = OllamaManager(log_dir=cfg.data / "logs")

    def load(self) -> None:
        from .log import log
        t = time.perf_counter()
        # Start-up has hung twice (5:41 pm, 8:54 pm on Nov 5) with nothing in the log, holding the index
        # open so that every writer got "database is locked". Log each step, and if loading is not done
        # after LOAD_DUMP_S, write every thread's stack to the log so the hang can be located.
        import faulthandler
        dump = None
        for h in log.handlers:
            if getattr(h, "stream", None) is not None and getattr(h, "baseFilename", None):
                dump = h.stream
        if dump is not None:
            faulthandler.dump_traceback_later(float(os.environ.get("LIBRARIAN_MCP_LOAD_DUMP", "120")), file=dump)
        step = lambda what: log.info("mcp: load: %s (%.1fs)", what, time.perf_counter() - t)  # noqa: E731
        try:
            step("importing models code")
            from . import embed
            from .index import has_vec
            from .spell import Vocab
            db = self.cfg.db_path
            if not db.is_file():
                raise FileNotFoundError(f"no index at {db}")
            step("opening the index")
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=30)
            if not has_vec(con):
                log.warning("sqlite-vec not loadable: semantic search falls back to BLOB vectors if present")
            self.con = con
            try:
                from .search import PageText
                step("page map")
                self.pages = PageText(con)
            except sqlite3.Error as e:   # searches then read the page files, as before
                self.warnings.append(f"page text not read from the index ({e}): using page files")
                log.warning("mcp: %s", self.warnings[-1])
            spec =(con.execute("SELECT value FROM meta WHERE key='embed_spec'").fetchone() or [None])[0]
            try:   # without the models, search still works on keywords (FTS)
                step("embedder and reranker")
                if spec:
                    self.emb = embed.load(spec)
                self.rr = embed.load_reranker(None)
            except Exception as e:  # noqa: BLE001
                self.warnings.append(f"models not loaded ({type(e).__name__}: {e}): keyword search only")
                log.warning("mcp: %s", self.warnings[-1])
            try:
                import torch
                self.device = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
            except ImportError:
                self.device = "cpu (no torch)"
            step("spelling vocabulary")
            self.speller = Vocab(con, db, self.cfg.data / "cache")
            self.loaded_in = round(time.perf_counter() - t, 1)
            log.info("mcp: ready in %.1fs (embed=%s, rerank=%s, device=%s)", self.loaded_in, spec,
                     getattr(self.rr, "model", None), self.device)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            log.exception("mcp: loading failed")
        finally:
            faulthandler.cancel_dump_traceback_later()
            self.ready.set()

    def load_vectors(self) -> None:
        """After start-up: copy every chunk vector into memory (GPU if there is one) for fast vector
        search. Searches meanwhile use the vectors in SQLite, so this never delays being ready."""
        from .log import log
        from . import memvec
        if self.error or self.emb is None:
            self.vectors_state = "off (no index or no embedder)"
            return
        device = memvec.wanted_device()
        if device is None:
            self.vectors_state = "off (LIBRARIAN_VEC_MEM=off)"
            return
        self.vectors_state = "loading"
        try:
            from .index import has_vec
            # Its own connection: tool calls keep using self.con while this reads ~6 GB.
            con = sqlite3.connect(self.cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                has_vec(con)
                self.vectors = memvec.load(con, self.emb.dim, device)
            finally:
                con.close()
            self.vectors_state = f"ready ({self.vectors.n} vectors on {self.vectors.device})"
        except Exception as e:  # noqa: BLE001 (search keeps working on SQLite's vectors)
            self.vectors_state = f"failed ({type(e).__name__}: {e}): using SQLite's vectors"
            log.exception("memvec: loading failed")

    def close(self) -> None:
        self.ollama.stop()
        if self.con is not None:
            self.con.close()
            self.con = None


def _resolve_book(con: sqlite3.Connection, book: str) -> tuple[Optional[tuple], list[tuple]]:
    """(sha, title, author, path) for a sha256 (prefix) or a title/author/path substring."""
    q = "SELECT sha256, title, author, path FROM books "
    rows = con.execute(q + "WHERE sha256 LIKE ?", (f"{book.lower()}%",)).fetchall() if len(book) >= 8 else []
    if not rows:
        rows = con.execute(q + "WHERE title LIKE ? OR author LIKE ? OR path LIKE ? ORDER BY title",
                           (f"%{book}%",) * 3).fetchall()
    exact = [r for r in rows if (r[1] or "").lower() == book.lower()]
    if len(exact) == 1:
        return exact[0], rows
    return (rows[0] if len(rows) == 1 else None), rows


def _pdf_uri(cfg, path: str, page: int) -> Optional[str]:
    src = cfg.sources / path
    try:
        return src.resolve().as_uri() + (f"#page={page}" if path.lower().endswith(".pdf") else "")
    except ValueError:
        return None


def loading_reply(st: "State") -> dict:
    """Answer for a tool call that arrives before start-up has finished."""
    secs = round(time.perf_counter() - st.started, 1)
    return {"loading": True, "loading_for_s": secs,
            "message": f"librarian is still loading its index and models ({secs:.0f}s so far; usually about "
                       "a minute). Call status until ready is true, then retry this call."}


READ_TOOLS = frozenset({"search", "read_page", "list_books", "contents", "subjects", "status"})


def build(state_holder: dict, operations: Optional[dict] = None):
    from mcp.server.fastmcp import FastMCP
    import anyio

    @asynccontextmanager
    async def lifespan(_server):
        from . import config
        from .log import log, setup
        cfg = config.load(None)
        setup(cfg.data, "mcp")
        log.propagate = False                     # the MCP SDK configures the root logger too
        st = State(cfg)
        state_holder["state"] = st
        atexit.register(st.ollama.stop)          # backup if the lifespan is cut short
        def load_all():
            st.load()
            st.load_vectors()
        threading.Thread(target=load_all, name="librarian-load", daemon=True).start()
        try:
            yield st
        finally:
            st.close()

    app = FastMCP("librarian", instructions=INSTRUCTIONS, lifespan=lifespan)

    def tool():
        """Export shared read operations without exposing management tools to HTTP."""
        def register(fn):
            if operations is not None and fn.__name__ in READ_TOOLS:
                operations[fn.__name__] = fn
            return app.tool()(fn)
        return register

    async def run(fn):
        st: State = state_holder["state"]
        if not st.ready.is_set():
            await anyio.to_thread.run_sync(st.ready.wait, LOAD_WAIT_S)
        if not st.ready.is_set():
            return loading_reply(st)
        if st.error:
            return {"error": f"librarian failed to load: {st.error}"}

        def locked():
            with st.lock:
                return fn(st)
        return await anyio.to_thread.run_sync(locked)

    @tool()
    async def search(query: str, k: int = 10, author: Optional[str] = None, book: Optional[str] = None,
                     chapter: Optional[str] = None, section: Optional[str] = None, mode: str = "hybrid",
                     expand: bool = False, rerank: bool = True, subject: Optional[str] = None) -> dict:
        """Search the library. Returns hits with book, chapter › section, printed and PDF page, line span,
        snippet, sha256 (for read_page) and a pdf link.

        query: words, a phrase in double quotes, or a description. Misspellings are corrected against the
          library's own vocabulary (reported in `corrected`).
        subject: subject folders to search in, comma-separated: "Real Analysis", "Metric Spaces, Topology".
          A name includes its subfolders ("Analysis" = real, complex, functional, metric spaces, ...;
          not "Numerical Analysis"); a path ("Analysis/Real Analysis") picks one folder. The subjects
          tool lists the names. The scope in the reply shows which folders matched.
        author / book: substring of the author, or of the title / file path (or a sha256 prefix).
        chapter / section: a number ("5", "5.4") or title words ("Compactness"); with neither book nor
          author this matches that chapter/section title in every book.
        mode: hybrid (default), phrase (exact phrase), fts (keywords only), semantic (meaning only).
        expand: ask the local Ollama model to add technical terms first (starts Ollama if needed; slow
          on first use). Usually better: search again with the terms yourself.
        rerank: false skips the cross-encoder (faster, worse ordering)."""
        def go(st: State):
            from . import embed, search as S
            info: dict = {}
            ex = None
            if expand and mode in ("hybrid", "semantic"):
                if st.ollama.ensure():
                    ex = embed.Expander(st.ollama.model)
                else:
                    info["expand"] = f"unavailable ({st.ollama.error}): searched without it"
            hits = S.search(st.cfg, st.con, query, st.emb if mode in ("hybrid", "semantic") else None, mode,
                            max(1, min(k, 50)), book, st.rr if rerank and mode in ("hybrid", "semantic") else None,
                            0, ex, author=author, chapter=chapter, section=section, speller=st.speller, info=info,
                            vectors=st.vectors, pages=st.pages, subject=subject)
            out = {"query": info.pop("query", query), **info, "timing_s": dict(S.LAST_TIMING), "hits": []}
            if st.emb is None and mode in ("hybrid", "semantic"):
                out["warning"] = "embedder not loaded: keyword (FTS) results only"
            for h in hits:
                out["hits"].append({
                    "title": h.title, "author": h.author, "chapter": h.chapter, "section": h.section,
                    "printed_page": h.printed_page, "pdf_page": h.pdf_page, "lines": [h.line_start, h.line_end],
                    "snippet": h.snippet, "score": h.score, "via": h.via, "sha256": h.sha256, "pdf": h.pdf_uri})
            return out
        return await run(go)

    @tool()
    async def read_page(book: str, pdf_page: Optional[int] = None, printed_page: Optional[str] = None,
                        line_start: Optional[int] = None, line_end: Optional[int] = None, context: int = 6) -> dict:
        """Read a page of a book, lines numbered as in search hits. Do this before asserting what a book says.

        book: sha256 (from a hit) or a title/author substring that names one book.
        pdf_page or printed_page ("17", "xiv"): which page. line_start/line_end: focus on these lines,
        with `context` lines either side (omit for the whole page). Pages next to it: call again with pdf_page±1."""
        def go(st: State):
            from .sections import locate
            hit, rows = _resolve_book(st.con, book)
            if hit is None:
                return {"error": "no book matches" if not rows else "several books match; pass a sha256",
                        "candidates": [{"title": r[1], "author": r[2], "sha256": r[0]} for r in rows[:15]]}
            sha, title, author, path = hit
            p, note = pdf_page, None
            if p is None:
                if printed_page is None:
                    return {"error": "give pdf_page or printed_page"}
                found = [r[0] for r in st.con.execute(
                    "SELECT pdf_page FROM pages WHERE sha256=? AND lower(printed_page)=lower(?) ORDER BY pdf_page",
                    (sha, str(printed_page)))]
                if not found:
                    return {"error": f"printed page {printed_page} not found in the page map", "title": title}
                p = found[0]
                if len(found) > 1:
                    note = f"printed page {printed_page} occurs at PDF pages {found}; showing the first"
            f = st.cfg.objects / sha / "pages" / f"{int(p):04d}.md"
            if not f.is_file():
                return {"error": f"PDF page {p} has no extracted text", "title": title}
            lines = f.read_text(encoding="utf-8").split("\n")
            a, b = 1, len(lines)
            if line_start is not None:
                a = max(1, line_start - max(0, context))
                b = min(len(lines), (line_end or line_start) + max(0, context))
            row = st.con.execute("SELECT printed_page, method, quality FROM pages WHERE sha256=? AND pdf_page=?",
                                 (sha, p)).fetchone()
            last = st.con.execute("SELECT max(pdf_page) FROM pages WHERE sha256=?", (sha,)).fetchone()[0]
            loc = locate(st.con, sha, int(p))
            out = {"title": title, "author": author, "sha256": sha, "pdf_page": p, "of_pdf_pages": last,
                   "printed_page": row[0] if row else None, "chapter": loc.get("chapter"), "section": loc.get("section"),
                   "text_source": row[1] if row else None, "quality": round(row[2], 2) if row and row[2] is not None else None,
                   "lines": [a, b], "text": "\n".join(f"{i:3d}| {lines[i - 1]}" for i in range(a, b + 1)),
                   "pdf": _pdf_uri(st.cfg, path, int(p))}
            if note:
                out["note"] = note
            return out
        return await run(go)

    @tool()
    async def list_books(filter: Optional[str] = None, author: Optional[str] = None, limit: int = 50,
                         subject: Optional[str] = None, offset: int = 0) -> dict:
        """List indexed books. filter: substring of title or file path (the path holds the subject folders,
        e.g. "Analysis/Functional"); author: substring of the author; subject: subject folders, as for
        search ("Metric Spaces, Topology")."""
        def go(st: State):
            from .search import subject_books
            where, args = ["b.sha256 IN (SELECT sha256 FROM index_state)"], []
            if subject and subject.strip():
                shas, _, unknown = subject_books(st.con, subject)
                if not shas:
                    return {"total": 0, "books": [], "unknown_subjects": unknown,
                            "note": "no subject folder matched: see the subjects tool for the names"}
                where.append(f"b.sha256 IN ({','.join('?' * len(shas))})")
                args += sorted(shas)
            if filter:
                where.append("(b.title LIKE ? OR b.path LIKE ?)")
                args += [f"%{filter}%"] * 2
            if author:
                where.append("b.author LIKE ?")
                args.append(f"%{author}%")
            w = " AND ".join(where)
            total = st.con.execute(f"SELECT count(*) FROM books b WHERE {w}", args).fetchone()[0]
            rows = st.con.execute(f"SELECT b.title, b.author, b.path, b.pdf_pages, b.sha256 FROM books b WHERE {w} "
                                  f"ORDER BY b.path LIMIT ? OFFSET ?", args + [max(1, min(limit, 500)), max(0, offset)]).fetchall()
            return {"total": total, "books": [{"title": r[0], "author": r[1], "folder": str(Path(r[2]).parent).replace("\\", "/"),
                                                "pdf_pages": r[3], "sha256": r[4]} for r in rows]}
        return await run(go)

    @tool()
    async def find_books(query: str, subject: Optional[str] = None, k: int = 20) -> dict:
        """Books about a topic, judged by their own title, table of contents and preface/introduction
        (not by page text): "Fourier series", "stochastic differential equations", "Lebesgue integral".
        Plain words must all appear; use OR between alternatives ("Hilbert OR Banach"). subject:
        restrict to subject folders, as for search. Then use profile for one book, search for passages."""
        def go(st: State):
            from . import profiles
            from .search import subject_books
            shas = None
            if subject and subject.strip():
                shas, _, unknown = subject_books(st.con, subject)
                if not shas:
                    return {"books": [], "unknown_subjects": unknown,
                            "note": "no subject folder matched: see the subjects tool for the names"}
            try:
                found = profiles.find(st.con, query, k, shas)
            except sqlite3.OperationalError as e:
                return {"error": f"no book profiles in the index yet (librarian profiles builds them): {e}"}
            for b in found:
                b["sha256"] = b["sha256"][:16]
            return {"books": found}
        return await run(go)

    @tool()
    async def profile(book: str) -> dict:
        """One book's profile: its table of contents (chapters) and its preface or introduction text.
        book: sha256 (prefix) or a title/author substring that names one book."""
        def go(st: State):
            from . import profiles
            hit, rows = _resolve_book(st.con, book)
            if hit is None:
                return {"error": "no book matches" if not rows else "several books match; pass a sha256",
                        "candidates": [{"title": r[1], "author": r[2], "sha256": r[0][:16]} for r in rows[:15]]}
            try:
                p = profiles.load(st.con, hit[0])
            except sqlite3.OperationalError:
                p = None
            return p or {"error": f"no profile for {hit[1]!r} (not indexed, or librarian profiles not run)"}
        return await run(go)

    @tool()
    async def subjects() -> dict:
        """The library's subject folders (for the subject filter of search and list_books), with the number
        of indexed books in each, subfolders included."""
        def go(st: State):
            from .search import subjects as all_subjects
            return {"subjects": all_subjects(st.con)}
        return await run(go)

    @tool()
    async def contents(book: str, depth: int = 2) -> dict:
        """Table of contents of one book: chapters (and sections down to `depth`: 1 chapters, 2 sections,
        3 subsections) with printed and PDF page ranges. book: sha256 or a title/author substring."""
        def go(st: State):
            hit, rows = _resolve_book(st.con, book)
            if hit is None:
                return {"error": "no book matches" if not rows else "several books match; pass a sha256",
                        "candidates": [{"title": r[1], "author": r[2], "sha256": r[0]} for r in rows[:15]]}
            try:
                secs = st.con.execute("SELECT level, number, title, printed_page, pdf_start, pdf_end FROM sections "
                                      "WHERE sha256=? AND level<=? ORDER BY seq", (hit[0], depth)).fetchall()
            except sqlite3.OperationalError:
                secs = []
            return {"title": hit[1], "author": hit[2], "sha256": hit[0],
                    "contents": [{"level": s[0], "number": s[1], "title": s[2], "printed_page": s[3],
                                  "pdf_pages": [s[4], s[5]]} for s in secs] or "no contents parsed for this book"}
        return await run(go)

    @tool()
    async def intake_check(run: bool = False) -> dict:
        """New books waiting in the inbox folders (Downloads, Drive 'Saved from Chrome' and 'Input'):
        the last check's counts (new, duplicate of a library book, possible other edition, paper, ...),
        the new books, and whether a check is running. run=true starts a fresh check in the
        background (a few minutes; hashing downloads online-only Drive files) and returns at once:
        call again later for the result. This tool never deletes anything."""
        st: State = state_holder["state"]
        return await anyio.to_thread.run_sync(lambda: _intake_check(st.cfg, run))

    @tool()
    async def ingest(run: bool = False) -> dict:
        """New books from the inbox folders. run=true starts `librarian ingest` in the background:
        exact duplicates deleted, papers moved to the papers folder, then each remaining book triaged
        by Codex (library subject or the hold folder, other editions, worse copies). It takes about
        half a minute per book; call again (run=false) for progress and the result: the plan as
        Markdown with numbered items and links. Nothing is filed until store(apply=true)."""
        st: State = state_holder["state"]
        return await anyio.to_thread.run_sync(lambda: _background(st.cfg, "ingest", ["ingest"], run, "ingest.md"))

    @tool()
    async def ingest_set(item: int, subject: Optional[str] = None, action: Optional[str] = None,
                         title: Optional[str] = None, author: Optional[str] = None,
                         surnames: Optional[str] = None, edition: Optional[str] = None) -> dict:
        """Adjust one item of the ingest plan (its number in the ingest report): subject (primary,
        exactly as listed by the subjects tool), action (library | edition | hold | delete), title,
        author, surnames (for the file name), edition. Returns where it will now go."""
        st: State = state_holder["state"]
        if action not in (None, "library", "edition", "hold", "delete"):
            return {"error": "action must be library, edition, hold or delete"}

        def go():
            from . import ingest as ing
            from .db import connect
            con = connect(st.cfg.db_path)
            try:
                return ing.set_item(st.cfg, con, item, action=action, primary=subject, title=title,
                                    author=author, surnames=surnames, edition=edition)
            finally:
                con.close()
        try:
            return await anyio.to_thread.run_sync(go)
        except SystemExit as e:
            return {"error": str(e)}

    @tool()
    async def store(apply: bool = False) -> dict:
        """Carry out the ingest plan. apply=false: what would happen (counts). apply=true starts
        `librarian store --apply` in the background: books copied into their subject folders (inbox
        originals deleted after the copy is verified), others to the hold folder, worse copies deleted,
        tags added, the new books indexed and queued for OCR (handed to a running OCR batch, or a new
        batch window started). Call store() again for the result."""
        st: State = state_holder["state"]
        if not apply:
            def dry():
                from . import ingest as ing
                from .db import connect
                con = connect(st.cfg.db_path)
                try:
                    return ing.store(st.cfg, con, apply=False)
                finally:
                    con.close()
            try:
                return await anyio.to_thread.run_sync(dry)
            except SystemExit as e:
                return {"error": str(e)}
        return await anyio.to_thread.run_sync(lambda: _background(st.cfg, "store", ["store", "--apply"], True, None))

    @tool()
    async def status() -> dict:
        """Index and server state: build date, books and pages indexed, models and GPU, Ollama."""
        st: State = state_holder["state"]
        base = {"ready": st.ready.is_set(), "error": st.error, "warnings": st.warnings, "loaded_in_s": st.loaded_in, "device": st.device,
                "vectors_in_memory": st.vectors_state, "ollama": st.ollama.state()}
        if not st.ready.is_set():
            return {**base, "loading_for_s": round(time.perf_counter() - st.started, 1)}
        if st.con is None:
            return base

        def go(st: State):
            meta = dict(st.con.execute("SELECT key, value FROM meta").fetchall())
            return {**base, "books_indexed": st.con.execute("SELECT count(*) FROM index_state").fetchone()[0],
                    "pages": st.con.execute("SELECT count(*) FROM pages").fetchone()[0],
                    "embedder": getattr(st.emb, "spec", None), "reranker": getattr(st.rr, "model", None),
                    "spell_words": sum(len(v) for v in st.speller.by_key.values()) if st.speller else 0,
                    "last_indexed": st.con.execute("SELECT max(indexed_at) FROM index_state").fetchone()[0],
                    "index": {k: v for k, v in meta.items() if k in ("schema_version", "embed_spec", "inventory_at",
                                                                     "vector_backend")}}
        return await run(go)

    return app


def _intake_running(pidfile: Path) -> Optional[dict]:
    """The background check started by intake_check(run=true), if it is still running."""
    import json as _json
    import subprocess
    try:
        info = _json.loads(pidfile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = int(info.get("pid", 0))
    if os.name == "nt":   # os.kill(pid, 0) would terminate the process on Windows
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        alive = str(pid) in out
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    return info if alive else None


def _intake_check(cfg, run: bool) -> dict:
    import json as _json
    import subprocess
    rep = cfg.reports
    pidfile, summary_file, md = rep / "intake-check.pid", rep / "intake-check.json", rep / "intake-check.md"
    out: dict = {}
    running = _intake_running(pidfile)
    if run and not running:
        rep.mkdir(parents=True, exist_ok=True)
        logf = open(cfg.data / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-intake-check.out", "w", encoding="utf-8")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        p = subprocess.Popen([sys.executable, "-m", "librarian", "intake", "check"], cwd=REPO, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
        running = {"pid": p.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        pidfile.write_text(_json.dumps(running), encoding="utf-8")
        out["started"] = True
    if running:
        out["running"] = running
    if summary_file.is_file():
        out["last_check"] = _json.loads(summary_file.read_text(encoding="utf-8"))
        if md.is_file():
            out["report"] = md.read_text(encoding="utf-8")[:12000]
    elif not running:
        out["note"] = "no check has run yet: call intake_check(run=true)"
    return out


def _background(cfg, name: str, args: list, run: bool, report: Optional[str]) -> dict:
    """Start `librarian <args>` detached (once at a time per name) and report on it and its last output."""
    import json as _json
    import subprocess
    rep = cfg.reports
    rep.mkdir(parents=True, exist_ok=True)
    pidfile = rep / f"{name}.pid"
    running = _intake_running(pidfile)
    out: dict = {}
    if run and not running:
        log = cfg.data / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.out"
        logf = open(log, "w", encoding="utf-8")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        p = subprocess.Popen([sys.executable, "-m", "librarian", *args], cwd=REPO, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
        running = {"pid": p.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "output": str(log)}
        pidfile.write_text(_json.dumps(running), encoding="utf-8")
        out["started"] = True
    if running:
        out["running"] = running
        try:
            tail = Path(running.get("output", "")).read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            out["progress"] = tail
        except OSError:
            pass
    elif pidfile.is_file():
        try:
            info = _json.loads(pidfile.read_text(encoding="utf-8"))
            out["last_run_output"] = Path(info["output"]).read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        except (OSError, ValueError, KeyError):
            pass
    if report and (rep / report).is_file():
        out["report"] = (rep / report).read_text(encoding="utf-8")[:30000]
    return out


def main() -> int:
    os.chdir(REPO)
    load_env(REPO / ".env")
    # Keep the protocol channel to ourselves: fd 1 and sys.stdout now go to stderr.
    proto_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    import anyio
    from mcp.server.stdio import stdio_server

    holder: dict = {}
    app = build(holder)

    async def serve():
        stdin = anyio.wrap_file(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
        stdout = anyio.wrap_file(io.TextIOWrapper(proto_out, encoding="utf-8", newline="\n", write_through=True))
        async with stdio_server(stdin, stdout) as (r, w):
            server = app._mcp_server   # FastMCP's low-level server (carries the lifespan)
            await server.run(r, w, server.create_initialization_options())

    anyio.run(serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
