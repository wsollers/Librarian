"""OCR confidence check through Lean: statement -> literal Lean 4 + Mathlib (proofs `sorry`) -> compile,
plus a blind back-translation compared with the OCR text.

Stages (each writes JSONL into one work dir, so stages can run on different machines):
  statements  page files -> statements.jsonl      {id, kind, label, pdf_page, line, text}
  formalize   Codex, from OCR text only          -> formal.jsonl     {id, lean, repairs, notes}
  backcheck   Codex, sees Lean first             -> back.jsonl       {id, english, verdict, diff}
  compile     lake env lean (Mathlib)            -> compile.jsonl    {id, ok, errors, counterexample}
  report      all of the above                   -> report.md / report.csv
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

try:
    from .log import log
except ImportError:  # standalone use (compile stage on a machine without librarian)
    import logging
    log = logging.getLogger("leancheck")
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

KINDS = r"Theorem|Lemma|Proposition|Corollary|Definition|Claim"
# Heading at line start; tolerates CAPS and OCR damage in the keyword (Derinition, THEOREM 49,).
_HEAD = re.compile(
    rf"^\s*[*_]{{0,2}}(?P<kind>{KINDS}|{KINDS.upper()}|De\w{{0,3}}inition|DE\w{{0,3}}INITION)"
    r"\b\s*(?P<num>[\dA-Z]+(?:\.\d+)*)?\s*[*_]{0,2}\s*[.,:]*\s*(?P<title>\([^)]{0,80}\))?\s*[.,:]?\s*(?P<rest>.*)$")
_STOP = re.compile(r"^\s*[*_]{0,2}(Proof|PROOF|Remark|REMARK|Example|EXAMPLE|Exercise|EXERCISE|Note|NOTE)\b")
MAX_LINES = 24
MAX_CHARS = 1500
# "Proposition 4.3.3 works just as well ..." is a reference, not a heading
_REF = re.compile(r"^(?:is|are|was|works|shows|says|states|gives|implies|holds|and|or|of|in|we|can|will|above|below|again|then|that|has|have|also|tells|may|lets|allows)\b")


def kind_of(word: str) -> str:
    w = word.lower()
    return "definition" if w.startswith("de") else w


_RUNNING_HEAD = re.compile(r"^\s*(\d{1,4}\s+\S.{0,70}|\S.{0,70}\s+\d{1,4})\s*$")
_ITEM = re.compile(r"^\s*\(?([a-h]|i{1,3}|iv|v|vi{0,3}|\d)\)\s")   # (a) ... (iii) ... 1) ... parts follow
_ENDS_SENTENCE = re.compile(r"[.!?)\]]\s*$")


def _stream(pages: dict[int, list[str]]) -> list[tuple[int, int, str]]:
    """All lines as (pdf_page, line_no, text), pages joined so a statement can run onto the next page.
    Running heads/feet (a short line with a page number at either end) at page edges are dropped."""
    out = []
    for p in sorted(pages):
        lines = pages[p]
        keep = list(range(len(lines)))
        for edge in (keep[:2], keep[-2:]):
            for k in edge:
                if _RUNNING_HEAD.match(lines[k]) and not _HEAD.match(lines[k]):
                    keep.remove(k) if k in keep else None
        out += [(p, k + 1, lines[k]) for k in keep]
    return out


def extract(pages: dict[int, list[str]], book: str) -> list[dict]:
    out = []
    stream = _stream(pages)
    i = 0
    while i < len(stream):
        p, ln_no, line = stream[i]
        m = _HEAD.match(line)
        # a reference like "Definition 4.3.4." alone on a line is not a statement
        if not m or (not m.group("rest") and not m.group("title") and i + 1 < len(stream)
                     and not stream[i + 1][2].strip()) or _REF.match(m.group("rest") or ""):
            i += 1
            continue
        block = [line.strip()]
        j = i + 1
        while j < len(stream) and len(block) < MAX_LINES:
            ln = stream[j][2]
            if _STOP.match(ln) or _HEAD.match(ln):
                break
            # a blank line ends the statement only after a finished sentence: displays follow "such that"
            if not ln.strip():
                nxt = next((t[2] for t in stream[j + 1:j + 4] if t[2].strip()), "")
                if (len(" ".join(block)) > 60 and _ENDS_SENTENCE.search(block[-1])
                        and not _ITEM.match(nxt)):
                    break
                j += 1
                continue
            block.append(ln.strip())
            j += 1
        text = re.sub(r"\s+", " ", " ".join(block)).strip()[:MAX_CHARS]
        if len(text) >= 40:
            label = f"{m.group('kind').capitalize()} {m.group('num') or ''}".strip()
            out.append({"id": f"s{len(out) + 1:03d}", "kind": kind_of(m.group("kind")), "label": label,
                        "pdf_page": p, "line": ln_no, "text": text, "book": book})
        i = j
    return out


FORMALIZE_PROMPT = """You are checking an OCR transcription of a mathematics textbook by formalizing it in Lean 4 with Mathlib.

Book: {book}
OCR text of one {kind} ({label}):
<<<
{text}
>>>

Write a Lean 4 + Mathlib formalization of exactly this {kind}, as LITERALLY as the text allows.
Rules:
- Statement only: every proof is `sorry`. A definition may be a `def`/`structure`/`abbrev`; if it defines a property, make it a `def ... : Prop`.
- Self-contained: `import Mathlib` is already present; you may add `open` lines and small local helper definitions for notions the text uses without defining. Do not write `namespace`.
- Use Mathlib concepts where they match (MetricSpace, CauchySeq, Filter.Tendsto, Subgroup, ...).
- Do NOT silently fix the OCR. If a symbol or word is garbled, missing, or you had to guess, still produce your best reading, but list EVERY guess in "repairs" (quote the OCR fragment and what you read it as).
- If the text is too garbled to formalize at all, set "lean" to "" and explain in "notes".

Reply with JSON only, no code fences:
{{"lean": "<Lean code>", "repairs": [{{"ocr": "...", "read_as": "..."}}], "notes": "<one sentence>"}}"""

BACK_PROMPT = """Step 1. Here is a Lean 4 + Mathlib statement (proofs are `sorry`). Translate it into precise mathematical English, as a textbook would state it. Do this before reading step 2.
<<<
{lean}
>>>

Step 2. Here is the OCR text the Lean was produced from:
<<<
{text}
>>>
Compare the mathematical content of your translation with the OCR text (ignore wording, notation and OCR noise; compare hypotheses, conclusions, quantifiers, relations and constants).

Reply with JSON only, no code fences:
{{"english": "<your step-1 translation>", "verdict": "same" | "minor" | "different", "diff": "<what differs, or empty>"}}"""


def _codex(prompt: str, timeout: int = 300) -> Optional[str]:
    exe = shutil.which(os.environ.get("LIBRARIAN_CODEX", "codex"))
    if not exe:
        raise SystemExit("codex CLI not found")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.txt"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", str(out)]
        if os.environ.get("LIBRARIAN_CODEX_MODEL"):
            cmd += ["--model", os.environ["LIBRARIAN_CODEX_MODEL"]]
        try:
            # prompt on stdin: multi-line arguments do not survive the codex.cmd shim on Windows
            subprocess.run(cmd + ["-"], cwd=td, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return out.read_text(encoding="utf-8", errors="replace") if out.is_file() else None


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


def _read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.is_file() else []


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _parallel(fn, rows, workers, label):
    done, out = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(fn, rows):
            out.append(r)
            done += 1
            if done % 5 == 0 or done == len(rows):
                log.info("  %s %d/%d", label, done, len(rows))
    return out


def formalize(work: Path, workers: int = 3) -> None:
    stmts = _read(work / "statements.jsonl")
    have = {r["id"] for r in _read(work / "formal.jsonl")}
    todo = [s for s in stmts if s["id"] not in have]

    def one(s):
        j = _json(_codex(FORMALIZE_PROMPT.format(**s))) or {}
        return {"id": s["id"], "lean": j.get("lean", ""), "repairs": j.get("repairs", []),
                "notes": j.get("notes", "" if j else "no/invalid reply")}
    rows = _read(work / "formal.jsonl") + _parallel(one, todo, workers, "formalize")
    _write(work / "formal.jsonl", sorted(rows, key=lambda r: r["id"]))


def backcheck(work: Path, workers: int = 3) -> None:
    stmts = {s["id"]: s for s in _read(work / "statements.jsonl")}
    formal = [f for f in _read(work / "formal.jsonl") if f["lean"].strip()]
    have = {r["id"] for r in _read(work / "back.jsonl")}
    todo = [f for f in formal if f["id"] not in have]

    def one(f):
        j = _json(_codex(BACK_PROMPT.format(lean=f["lean"], text=stmts[f["id"]]["text"]))) or {}
        return {"id": f["id"], "english": j.get("english", ""), "verdict": j.get("verdict", "error"),
                "diff": j.get("diff", "")}
    rows = _read(work / "back.jsonl") + _parallel(one, todo, workers, "backcheck")
    _write(work / "back.jsonl", sorted(rows, key=lambda r: r["id"]))


# ---------- compile (needs a Lake project with Mathlib) ----------

_ERR = re.compile(r"^.*?:(\d+):(\d+): error(?:\([^)]*\))?: (.*)$")


def _strip_imports(code: str) -> str:
    return "\n".join(l for l in code.splitlines() if not l.strip().startswith("import "))


def compile_all(work: Path, project: Path, plausible: bool = True) -> None:
    """One file per run: each statement in its own namespace; errors mapped back by line range."""
    formal = [f for f in _read(work / "formal.jsonl") if f["lean"].strip()]
    parts, ranges, line = ["import Mathlib", "set_option maxHeartbeats 400000", ""], {}, 3
    for f in formal:
        body = _strip_imports(f["lean"])
        block = [f"namespace LC_{f['id']}", body, f"end LC_{f['id']}", ""]
        ranges[f["id"]] = (line + 1, line + len("\n".join(block).splitlines()))
        parts += block
        line += len("\n".join(block).splitlines())
    src = work / "check.lean"
    src.write_text("\n".join(parts) + "\n", encoding="utf-8")
    r = subprocess.run(["lake", "env", "lean", str(src)], cwd=project, capture_output=True, text=True, timeout=1800)
    errors: dict[str, list[str]] = {}
    for ln in (r.stdout + r.stderr).splitlines():
        m = _ERR.match(ln)
        if m:
            n = int(m.group(1))
            sid = next((k for k, (a, b) in ranges.items() if a <= n <= b), None)
            if sid:
                errors.setdefault(sid, []).append(m.group(3)[:300])
    counter = {}
    if plausible:
        counter = _plausible(work, project, formal, errors)
    rows = [{"id": f["id"], "ok": f["id"] not in errors, "errors": errors.get(f["id"], []),
             "counterexample": counter.get(f["id"])} for f in formal]
    _write(work / "compile.jsonl", rows)
    log.info("compile: %d statements, %d compile, %d fail, %d counterexamples", len(rows),
             sum(r["ok"] for r in rows), sum(not r["ok"] for r in rows), sum(bool(r["counterexample"]) for r in rows))


_THM = re.compile(r"(?ms)^(theorem|lemma)\s+(\S+)(.*?):=\s*by\s+sorry")


def _plausible(work, project, formal, errors) -> dict[str, str]:
    """Replace `sorry` by `plausible` in compiling theorems; a found counterexample means the statement is false."""
    parts, ranges, line = ["import Mathlib", ""], {}, 2
    for f in formal:
        if f["id"] in errors or not _THM.search(f["lean"]):
            continue
        body = _THM.sub(lambda m: f"{m.group(1)} {m.group(2)}{m.group(3)}:= by\n  plausible (config := {{ numInst := 200 }})",
                        _strip_imports(f["lean"]))
        block = [f"namespace LP_{f['id']}", body, f"end LP_{f['id']}", ""]
        ranges[f["id"]] = (line + 1, line + len("\n".join(block).splitlines()))
        parts += block
        line += len("\n".join(block).splitlines())
    if not ranges:
        return {}
    src = work / "plausible.lean"
    src.write_text("\n".join(parts) + "\n", encoding="utf-8")
    r = subprocess.run(["lake", "env", "lean", str(src)], cwd=project, capture_output=True, text=True, timeout=1800)
    # An error line is followed by its message; plausible's reads "Found a counter-example!" then the values.
    found, cur, in_ce = {}, None, False
    for ln in (r.stdout + r.stderr).splitlines():
        m = _ERR.match(ln)
        if m:
            n = int(m.group(1))
            cur = next((k for k, (a, b) in ranges.items() if a <= n <= b), None)
            in_ce = bool(cur) and "counter-example" in ln.lower()
            if in_ce:
                found[cur] = ""
        elif cur and "counter-example" in ln.lower():
            in_ce, found[cur] = True, ""
        elif in_ce and ln.strip() and not set(ln.strip()) <= set("=-") and len(found[cur]) < 200:
            found[cur] += (" " if found[cur] else "") + ln.strip()
    return found


# ---------- report ----------

def report(work: Path) -> dict:
    S = {s["id"]: s for s in _read(work / "statements.jsonl")}
    F = {r["id"]: r for r in _read(work / "formal.jsonl")}
    B = {r["id"]: r for r in _read(work / "back.jsonl")}
    C = {r["id"]: r for r in _read(work / "compile.jsonl")}
    rows = []
    for sid, s in S.items():
        f, b, c = F.get(sid, {}), B.get(sid, {}), C.get(sid, {})
        reasons = []
        if not f.get("lean"):
            reasons.append("not formalizable: " + f.get("notes", ""))
        elif not c.get("ok", False):
            reasons.append("does not compile: " + "; ".join(c.get("errors", [])[:2]))
        if c.get("counterexample") is not None:
            reasons.append("counterexample: " + c["counterexample"][:120])
        if f.get("repairs"):
            reasons.append(f"{len(f['repairs'])} OCR repair(s): " +
                           "; ".join(f"'{r.get('ocr', '')}'->'{r.get('read_as', '')}'" for r in f["repairs"][:3]))
        if b.get("verdict") in ("different", "minor"):
            reasons.append(f"back-check {b['verdict']}: {b.get('diff', '')[:160]}")
        hard = (not f.get("lean")) or (not c.get("ok", False)) or c.get("counterexample") is not None \
            or b.get("verdict") == "different"
        conf = "low" if hard else ("medium" if f.get("repairs") or b.get("verdict") == "minor" else "high")
        rows.append({"id": sid, "label": s["label"], "kind": s["kind"], "pdf_page": s["pdf_page"],
                     "line": s["line"], "confidence": conf, "reasons": " | ".join(reasons),
                     "text": s["text"][:300], "lean": f.get("lean", "")})
    n = len(rows)
    count = {k: sum(r["confidence"] == k for r in rows) for k in ("high", "medium", "low")}
    md = [f"# Lean confidence check: {work.name}", "",
          f"{n} statements: {count['high']} high, {count['medium']} medium, {count['low']} low confidence.", "",
          "| id | statement | PDF p. | confidence | reasons |", "|---|---|---:|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['label']} | {r['pdf_page']} | {r['confidence']} | "
                  f"{r['reasons'].replace('|', '/')[:400]} |")
    (work / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with (work / "report.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
    return {"statements": n, **count, "report": str(work / "report.md")}
