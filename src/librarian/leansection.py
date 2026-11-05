"""Section-level OCR check: one Codex session per chunk of a section, with the page images and the
Lean tools of lean-lsp-mcp (registered in Codex as `lean`; see scripts/lean-setup.ps1).

Codex reads every statement from the page image, compares it with our OCR lines, formalizes it in
Lean (statements only, proofs `sorry`), fixes its Lean with the Lean diagnostics, and reports OCR
issues with page/line, plus numbering gaps. The harness then compiles each chunk's Lean file itself
(trust, but verify) and writes:

  <data>/leancheck/<name>/cNN/{pages.md, pNNNN.png, prompt.txt, reply.txt, result.json, verify.json}
  <data>/leancheck/<name>/{report.md, report.csv, corrections.jsonl}
  <lean project>/LeanCheck/<name>/CNN.lean

Nothing in the page files is changed: corrections.jsonl is a proposal to review/apply later.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

from .log import log

PROMPT = """You are checking the OCR of a mathematics textbook against the page images, using Lean 4 + Mathlib through the tools of the `lean` MCP server.

Book: {book}
Section: {section}
Page images are attached in this order: PDF pages {pages}.
OCR text of those pages follows. Every line is prefixed "page:line|" (PDF page, 1-based line of our page file).
<<<
{ocr}
>>>
{context}
Lean: the current directory is a Lean project with Mathlib. Write your Lean to {lean_file} (create or overwrite it).
Start it with these lines exactly:
import Mathlib
open Filter Topology
noncomputable section
and end it with a final line `end`.
If you cannot write files here, check your Lean with the lean tool that runs a code snippet instead, and put the final file content in the "lean" field of your reply (always fill that field anyway).

Task
1. Find every Definition, Theorem, Lemma, Proposition, Corollary and Claim on these pages (numbered or not). A statement may continue onto the next page.
2. Read each statement from the IMAGE; the image is authoritative. Compare it with the OCR lines and record every difference that matters for mathematics or search: a garbled or wrong symbol, letter, digit, operator, sub/superscript, a missing or extra word, a dropped line. Use kind "content" when the OCR changes or loses meaning, "layout" when it only obscures it (flattened sub/superscripts, hyphenation, spacing). Also record content errors you notice elsewhere on the pages (examples, proofs, displays) under other_issues.
3. Formalize each statement as literally as the image allows: statement only, every proof `sorry`; a definition is a `def` (a `def ... : Prop` for a property) or `structure`. Name declarations like theorem_45, lemma_6_4_13, definition_p98_1. Use Mathlib notions where they match. Use only Lean 4 commands (theorem, lemma, def, structure, abbrev, instance, variable, open); never `proposition` or `corollary` as keywords.
4. Check the Lean with the lean tools (diagnostics of {lean_file}); search Mathlib (local search / leanfinder / loogle) for names you are unsure of; fix until the file has no errors. Warnings and `sorry` are fine. If a statement cannot reasonably be formalized, skip it and say why; never invent content that is not on the page.
5. Numbering: list the statement numbers printed on these pages, and any gaps or duplicates.
Do not modify any file other than {lean_file}. Do not prove anything.

Reply with JSON only, no code fences:
{{"lean": "<full content of {lean_file}>",
 "statements": [{{"label": "Theorem 45", "kind": "theorem", "pdf_page": 99, "line": 12, "lean_name": "theorem_45", "formalized": true, "compiles": true, "image_matches_ocr": false, "issues": [{{"pdf_page": 99, "line": 13, "ocr": "%2", "image": "x_2", "kind": "content"}}], "note": ""}}],
 "numbering": {{"seen": ["43", "44"], "gaps": [], "duplicates": []}},
 "other_issues": [{{"pdf_page": 99, "line": 20, "ocr": "...", "image": "...", "kind": "content"}}],
 "summary": "<one sentence>"}}"""

_DECL = re.compile(r"^(?:noncomputable\s+|protected\s+|private\s+)*(theorem|lemma|def|structure|abbrev|instance|class)\s+([^\s:({\[]+)", re.M)
_DIAG = re.compile(r"^.*?:(\d+):(\d+): (error|warning)(?:\([^)]*\))?: (.*)$")


def lean_project(cfg) -> Path:
    return Path(os.environ.get("LIBRARIAN_LEAN_PROJECT", str(cfg.data / "lean")))


def plan_chunks(con: sqlite3.Connection, sha: str, lo: int, hi: int, max_pages: int) -> list[tuple[int, int, str]]:
    """Split [lo, hi] at section starts (deepest level known), then into pieces of at most max_pages."""
    rows = con.execute("SELECT level, number, title, pdf_start, pdf_end FROM sections WHERE sha256=? "
                       "AND pdf_end >= ? AND pdf_start <= ? ORDER BY pdf_start, level", (sha, lo, hi)).fetchall()
    starts = {lo: "pages %d-%d" % (lo, hi)}
    for level, number, title, a, b in rows:
        if lo <= a <= hi:
            starts[a] = f"{number or ''} {title}".strip()
    cuts = sorted(starts)
    out = []
    for i, a in enumerate(cuts):
        b = (cuts[i + 1] - 1) if i + 1 < len(cuts) else hi
        title = starts[a]
        if a == lo and len(starts) > 1 and rows:     # name the first piece after the enclosing section
            enclosing = [r for r in rows if r[3] <= lo <= r[4]]
            if enclosing:
                r = max(enclosing, key=lambda r: r[0])
                title = f"{r[1] or ''} {r[2]}".strip()
        n = b - a + 1
        pieces = -(-n // max_pages)
        size = -(-n // pieces)                       # even pieces: 22 pages at max 8 -> 8, 7, 7
        for s in range(a, b + 1, size):
            out.append((s, min(b, s + size - 1), title))
    return out


def _page_lines(obj: Path, p: int) -> list[str]:
    f = obj / "pages" / f"{p:04d}.md"
    return f.read_text(encoding="utf-8").split("\n")[:-1] if f.is_file() else []


def _json(reply: Optional[str]) -> Optional[dict]:
    if not reply:
        return None
    s = reply.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[i:j + 1]) if i >= 0 else None
    except json.JSONDecodeError:
        return None


def _verify(project: Path, rel: str) -> dict:
    """Compile the chunk's Lean file ourselves and map errors to declarations."""
    f = project / rel
    if not f.is_file():
        return {"exists": False, "errors": 0, "by_decl": {}}
    src = f.read_text(encoding="utf-8", errors="replace")
    decls = [(src.count("\n", 0, m.start()) + 1, m.group(2)) for m in _DECL.finditer(src)]
    t = time.perf_counter()
    r = subprocess.run(["lake", "env", "lean", rel], cwd=project, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    by_decl: dict[str, list[str]] = {}
    n_err = 0
    for ln in (r.stdout + r.stderr).splitlines():
        m = _DIAG.match(ln)
        if not m or m.group(3) != "error":
            continue
        n_err += 1
        line = int(m.group(1))
        owner = next((name for start, name in reversed(decls) if start <= line), "(preamble)")
        by_decl.setdefault(owner, []).append(m.group(4)[:200])
    return {"exists": True, "errors": n_err, "by_decl": by_decl, "decls": [d for _, d in decls],
            "seconds": round(time.perf_counter() - t, 1)}


def run_chunk(cfg, row, pdf: Path, work: Path, k: int, a: int, b: int, title: str, name: str,
              context: str, dpi: int, timeout: int, force: bool) -> dict:
    from .ocr import render_page
    cdir = work / f"c{k:02d}"
    res_file = cdir / "result.json"
    if res_file.is_file() and not force:
        return json.loads(res_file.read_text(encoding="utf-8"))
    cdir.mkdir(parents=True, exist_ok=True)
    obj = cfg.objects / row["sha256"]
    pages = list(range(a, b + 1))
    ocr = []
    for p in pages:
        ocr.append(f"=== PDF page {p} ===")
        ocr += [f"{p}:{i}|{t}" for i, t in enumerate(_page_lines(obj, p), 1)]
    (cdir / "pages.md").write_text("\n".join(ocr) + "\n", encoding="utf-8")
    images = [render_page(pdf, p, cdir, dpi=dpi) for p in pages]
    project = lean_project(cfg)
    rel = f"LeanCheck/{name}/C{k:02d}.lean"
    (project / rel).parent.mkdir(parents=True, exist_ok=True)
    if (project / rel).is_file():
        (project / rel).unlink()          # never verify a stale file from an earlier run
    prompt = PROMPT.format(book=f'{row["title"]} - {row["author"] or "Unknown"}', section=title,
                           pages=", ".join(map(str, pages)), ocr="\n".join(ocr), context=context, lean_file=rel)
    (cdir / "prompt.txt").write_text(prompt, encoding="utf-8")
    exe = shutil.which(os.environ.get("LIBRARIAN_CODEX", "codex"))
    if not exe:
        raise SystemExit("codex CLI not found")
    reply_file = cdir / "reply.txt"
    cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(project),
           "--output-last-message", str(reply_file)]
    for img in images:
        cmd += ["--image", str(img)]
    if os.environ.get("LIBRARIAN_CODEX_MODEL"):
        cmd += ["--model", os.environ["LIBRARIAN_CODEX_MODEL"]]
    cmd.append("-")
    log.info("  chunk %02d: PDF %d-%d (%s)", k, a, b, title)
    t = time.perf_counter()
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout)
        (cdir / "codex.log").write_bytes(r.stdout[-200_000:] + b"\n--- stderr ---\n" + r.stderr[-50_000:])
    except subprocess.TimeoutExpired:
        log.error("  chunk %02d: Codex timed out after %ds", k, timeout)
    secs = round(time.perf_counter() - t, 1)
    reply = reply_file.read_text(encoding="utf-8", errors="replace") if reply_file.is_file() else None
    res = _json(reply) or {"statements": [], "numbering": {}, "other_issues": [],
                           "summary": "no/invalid reply from Codex"}
    lean_text = res.pop("lean", "") or ""
    if lean_text.strip() and not (project / rel).is_file():
        (project / rel).write_text(lean_text, encoding="utf-8")
    res.update({"chunk": k, "pdf_start": a, "pdf_end": b, "section": title, "lean_file": rel,
                "codex_seconds": secs})
    res["verify"] = _verify(project, rel)
    res_file.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    n_iss = sum(len(s.get("issues", [])) for s in res["statements"]) + len(res.get("other_issues", []))
    log.info("  chunk %02d: %d statements, %d OCR issues, Lean errors %s, %.0fs",
             k, len(res["statements"]), n_iss, res["verify"].get("errors"), secs)
    return res


def _context(results: list[dict]) -> str:
    seen = [f"{s.get('label')} (PDF {s.get('pdf_page')}, Lean `{s.get('lean_name') or '-'}`)"
            for r in results for s in r.get("statements", [])]
    if not seen:
        return ""
    return ("Statements already checked earlier in this run (for references such as 'by Theorem 44'; "
            "their Lean is not imported, restate a needed definition locally):\n" + "; ".join(seen[-60:]) + "\n")


def _numbers(results: list[dict]) -> dict:
    """Numbering gaps/duplicates over the whole run, for plain integer numbering (Kaplansky: Theorem 43...)
    and dotted numbering (6.4.13) grouped by prefix."""
    nums: dict[str, list[int]] = {}
    for r in results:
        for s in r.get("statements", []):
            m = re.search(r"(\d+(?:\.\d+)*)\s*$", s.get("label", ""))
            if not m:
                continue
            parts = m.group(1).split(".")
            nums.setdefault(".".join(parts[:-1]), []).append(int(parts[-1]))
    gaps, dups = [], []
    for prefix, xs in nums.items():
        xs_sorted = sorted(xs)
        full = set(range(xs_sorted[0], xs_sorted[-1] + 1))
        p = f"{prefix}." if prefix else ""
        gaps += [f"{p}{n}" for n in sorted(full - set(xs))]
        dups += [f"{p}{n}" for n in sorted({x for x in xs if xs.count(x) > 1})]
    return {"gaps": gaps, "duplicates": dups}


def run(cfg, con: sqlite3.Connection, row, lo: int, hi: int, name: str, max_pages: int = 8, dpi: int = 150,
        timeout: int = 1800, force: bool = False) -> dict:
    work = cfg.data / "leancheck" / name
    work.mkdir(parents=True, exist_ok=True)
    pdf = cfg.sources / row["path"]
    chunks = plan_chunks(con, row["sha256"], lo, hi, max_pages)
    log.info("leancheck section %s: %s, PDF %d-%d, %d chunk(s)", name, row["title"], lo, hi, len(chunks))
    results = []
    for k, (a, b, title) in enumerate(chunks, 1):
        results.append(run_chunk(cfg, row, pdf, work, k, a, b, title, name, _context(results), dpi, timeout, force))
    return report(work, results)


def report(work: Path, results: Optional[list[dict]] = None) -> dict:
    if results is None:
        results = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(work.glob("c*/result.json"))]
    rows, corrections = [], []
    for r in results:
        v = r.get("verify", {})
        for s in r.get("statements", []):
            errs = v.get("by_decl", {}).get(s.get("lean_name") or "", [])
            content = [i for i in s.get("issues", []) if i.get("kind") == "content"]
            layout = [i for i in s.get("issues", []) if i.get("kind") != "content"]
            if not s.get("formalized"):
                conf = "low"
            elif errs or not v.get("exists"):
                conf = "low"
            elif content:
                conf = "medium"
            else:
                conf = "high"
            rows.append({"chunk": r["chunk"], "label": s.get("label", ""), "pdf_page": s.get("pdf_page"),
                         "line": s.get("line"), "lean_name": s.get("lean_name", ""),
                         "confidence": conf, "lean_errors": len(errs), "content_issues": len(content),
                         "layout_issues": len(layout), "note": s.get("note", ""),
                         "issues": "; ".join(f"p{i.get('pdf_page')}:{i.get('line')} '{i.get('ocr')}' -> '{i.get('image')}'"
                                             for i in s.get("issues", []))})
            corrections += [dict(i, label=s.get("label", "")) for i in s.get("issues", [])]
        corrections += [dict(i, label="(other)") for i in r.get("other_issues", [])]
    numbering = _numbers(results)
    codex_gaps = sorted({g for r in results for g in (r.get("numbering") or {}).get("gaps", [])}, key=str)
    codex_dups = sorted({g for r in results for g in (r.get("numbering") or {}).get("duplicates", [])}, key=str)
    count = {k: sum(x["confidence"] == k for x in rows) for k in ("high", "medium", "low")}
    secs = sum(r.get("codex_seconds", 0) for r in results)
    md = [f"# Section Lean check: {work.name}", "",
          f"{len(rows)} statements in {len(results)} chunk(s): {count['high']} high, {count['medium']} medium, "
          f"{count['low']} low. OCR issues: {sum(x['content_issues'] for x in rows)} content, "
          f"{sum(x['layout_issues'] for x in rows)} layout in statements; "
          f"{sum(len(r.get('other_issues', [])) for r in results)} elsewhere. Codex time {secs / 60:.1f} min.", "",
          f"Numbering read from the images by Codex: gaps {', '.join(map(str, codex_gaps)) or 'none'}; "
          f"duplicates {', '.join(map(str, codex_dups)) or 'none'}.",
          f"Numbers missing among the checked statements (may be examples/remarks sharing the numbering): "
          f"{', '.join(numbering['gaps']) or 'none'}; repeated: {', '.join(numbering['duplicates']) or 'none'}.", "",
          "| chunk | statement | PDF p. | line | confidence | Lean errors | OCR issues |", "|---|---|---:|---:|---|---:|---|"]
    for x in rows:
        md.append(f"| {x['chunk']} | {x['label']} | {x['pdf_page']} | {x['line']} | {x['confidence']} | "
                  f"{x['lean_errors']} | {x['issues'].replace('|', '/')[:300]} |")
    md += ["", "## Chunk summaries", ""]
    md += [f"- c{r['chunk']:02d} PDF {r['pdf_start']}-{r['pdf_end']} ({r.get('section', '')}): {r.get('summary', '')} "
           f"[Lean errors after Codex: {r.get('verify', {}).get('errors')}, {r.get('codex_seconds', 0):.0f}s]"
           for r in results]
    (work / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with (work / "report.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["chunk"])
        w.writeheader()
        w.writerows(rows)
    (work / "corrections.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in corrections),
                                            encoding="utf-8")
    return {"work": str(work), "statements": len(rows), **count, "corrections": len(corrections),
            "numbering_gaps": codex_gaps, "statement_gaps": numbering["gaps"], "duplicates": numbering["duplicates"],
            "codex_minutes": round(secs / 60, 1)}
