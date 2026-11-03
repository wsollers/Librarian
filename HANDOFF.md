# Librarian: handoff

Start here. Written 2026-11-03 by the AIM "sources" conversation, after a
survey of the existing LRA source stack on hal5000. Librarian is its own
project: library infrastructure that AIM, LRA and anything else consume.

## Goal (William's words, condensed)

* Every book he owns (≈905 in `D:\Readings\Sources`, many paid ebooks) is
  usable: full text extracted **line by line into Markdown with page breaks**,
  so every reference gives book, page and line.
* Search by **exact text** and by **description**. Acceptance query:
  *"What is the type of continuity where the change is bounded by a set
  constant value"* must surface Lipschitz-continuity definitions (Tao I et al.)
  with printed page, PDF page and lines. Full-text search alone cannot answer
  this; it needs embeddings.
* Keep the good parts of the old stack: ingest, cross-encode, DjVu conversion,
  nougat to improve OCR on math, LLM OCR where necessary, SQLite + FTS5, a
  vector index if useful (otherwise SQLite only).
* Once Librarian works, **retire the old lra-* source stack**, salvaging what
  is useful (PDFs, object cache, extractors).

## Decisions already made

| Topic | Decision |
|---|---|
| Repo | new `wsollers/librarian`; the lra-* repos are to be made private |
| Project | a separate claude.ai Project "Librarian" (orthogonal to AIM) |
| Old stack | leave running until Librarian is usable, then retire |
| Orchestration | plain Python CLI, incremental, cached by SHA-256; no Dagster, no volume routing, no review queues on the path from PDF to search |
| LLM OCR | any of the big four, preference OpenAI > Claude > Gemini > Copilot. **Do not spend Claude on OCR**; Claude is for reasoning |
| Sync | Google Drive for desktop (`G:\My Drive` on hal5000); mirror on zarathustra |
| Copyright | extracted text and indexes: private Drive only, never in git, never shared |
| First books | AIM logic core + Tao, then the rest. Enderton (Logic, Set Theory), Tarski *Introduction*, Mendelson 6th, Ebbinghaus–Flum–Thomas, Mileti, Manin *Logic*, Simpson, Stillwell, Tao I/II, Nederpelt–Geuvers *Type Theory and Formal Proof*; Feferman as a possible substitute |

## What the survey found (why the old system fails)

1. **Search only sees books routed into an LRA volume.**
   `D:\Readings\indexes\lra\omnibus\sqlite\omnibus-search.sqlite` (1.3 GB,
   FTS5, built by `lra-source-profiles/scripts/build_omnibus_sqlite_index.py`)
   indexes `volumes/*/_sources/markdown/SRC-*.md` (898 SRC ids). Books that
   were never assigned a volume/chapter are invisible even when extracted.
   Tao I, both Endertons, Tarski *Introduction*, Mendelson 6th, EFT, Manin,
   Nederpelt are all absent; "construction sequence" finds nothing in Enderton.
2. **Extraction coverage is ≈58% and silent.** 905 source files (902 PDF,
   2 DjVu, 1 EPUB); only 523 have library Markdown in
   `indexes\lra\library\markdown\LIB-<sha16>.md`. Nothing reports the rest.
3. **Two incompatible page formats.** `lra-pdf-extractor` output has
   `=== Page N ===` markers; plain `pdftotext` output (Enderton, Tarski,
   Mendelson, EFT, Mileti, Simpson…) uses form feeds and drops one or two, so
   page numbers drift. Re-running `pdftotext` per PDF splits exactly (Enderton
   330/330) in under a second.
4. **No printed pages anywhere.** Omnibus `page` = PDF page. Notes cite printed
   pages (Enderton offset 13: PDF 30 = printed 17).
5. Files are CRLF, some contain control characters (`grep` sees binary).

## Salvage list

| Asset | Where | Use |
|---|---|---|
| PDFs | `D:\Readings\Sources\<category>\…` | canonical input, never modified |
| object cache | `D:\Readings\indexes\lra\objects\pdf\<sha256>\` (2.1 GB: extraction, metadata.yaml) | seed; reuse OCR/nougat output already paid for |
| library Markdown | `indexes\lra\library\markdown\` (601 files) | seed for the 523 covered books; verify page counts against `pdfinfo` |
| manifest | `F:\repos\lra-source-profiles\data\source-manifest.yaml` | titles, authors, hashes |
| nougat runners | `lra-source-profiles/scripts/nougat_{chunked,queue}_runner.py` | reuse for math pages |
| OCR / text probe | `ocr_status.py`, `probe_source_text.py` | text-layer quality checks |
| embeddings | `semantic_lancedb.py`, model `BAAI/bge-small-en-v1.5` | starting point; evaluate a stronger model on math queries |
| LLM client | `lra-pdf-extractor/src/*/llm.py`, `ocr.py` (OpenAI default) | LLM OCR adapter |
| harness map | `lra-ingestion-harness/docs/architecture-and-operations.md` | where everything lives |

## Proposed design

| Stage | Does | Output |
|---|---|---|
| inventory | walk `D:\Readings\Sources`, SHA-256 each file, dedupe | `books` table; **coverage report** listing every book as indexed or failed-with-reason |
| extract | per page, escalate: text layer → quality score → nougat (math-heavy or garbled) → LLM OCR (still bad). DjVu via `djvutxt`/conversion; EPUB by section | `objects/<sha>/pages/NNNN.md`, line by line, cached; reruns are incremental |
| page map | printed page per PDF page from running heads/footers; per-book override file (offsets drift within a book) | PDF page ↔ printed page |
| index | one SQLite file: books, pages, lines, FTS5 (`unicode61 remove_diacritics 2`), chunk embeddings (`sqlite-vec` in the same file) | hybrid search: phrase via FTS, description via vectors, merged ranks |
| publish | build on hal5000, copy read-only to `G:\My Drive\…`, mirrored by Drive for desktop | portable index; schema version and build date inside |
| skill | find index (local mirror, or download from Drive into a cloud session), query, then **open the page itself before asserting anything** | citations: book, printed p., PDF p., lines |

Prefer one SQLite file over LanceDB for portability; revisit if too slow.

## Phases and acceptance

1. Inventory + coverage report for all 905 files.
2. Priority books end to end. Tests:
   * "construction sequence" → Enderton *Logic* §1.1, printed p. 17–18 (PDF 30–31);
   * the Lipschitz query above → Tao I and at least one other analysis text;
   * every hit carries printed page, PDF page, line span.
3. Whole library; coverage report shows zero silent gaps.
4. Search skill (proposed via the skill review card); AIM's `notes/CONVENTIONS.md`
   §10 updated by the AIM hub to point at it.
5. Retire the old source stack; make lra-* repos private (if not done).

## Open questions for William

* Drive folder for the published index (suggested `G:\My Drive\Librarian\`).
* Is zarathustra running Drive for desktop? (Only mirror target besides hal5000.)
* GPU on hal5000 for nougat/embeddings? Local embeddings via Ollama vs API.
* OCR budget ceiling per book for LLM OCR.
