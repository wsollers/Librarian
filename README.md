# Librarian

Library infrastructure for William's mathematics books (~905 files in
`D:\Readings\Sources`). Every book becomes **page-exact, line-numbered Markdown**,
searchable by exact phrase (SQLite FTS5) and by description (embeddings), and every
hit cites **book, printed page, PDF page and lines**.

Consumers: AdventuresInMathematics (AIM), LRA, and anything else. Background,
decisions and phases: [HANDOFF.md](HANDOFF.md).

> **Copyright.** Books, extracted text, indexes and reports live under the data
> directory (`D:\Readings\librarian`) and are published only to private Drive
> (`G:\My Drive\Librarian`). They are never committed: see `.gitignore`.

## Setup (hal5000, once)

```powershell
choco install poppler                  # pdftotext/pdftoppm/pdfimages
winget install --id DjVuLibre.DjView --exact   # djvutxt/djvused (found in Program Files (x86)\DjVuLibre)
scripts\bootstrap.ps1                  # .venv, CUDA torch, deps, requirements.lock, tool checks
codex login                            # LLM OCR uses the Codex CLI on your ChatGPT plan (default)
notepad .env                           # or LIBRARIAN_OCR_BACKEND=openai + OPENAI_API_KEY
```

`bootstrap.ps1` is the only supported way to build the environment; the CLI refuses to run
outside `.venv`. Commit `requirements.lock` after the first run; later runs install it exactly.

## Running

```powershell
scripts\run.ps1 run --select priority          # inventory → extract → pagemap → index
scripts\run.ps1 search "construction sequence"
scripts\run.ps1 search "type of continuity where the change is bounded by a constant"
scripts\run.ps1 run --select all --llm-ocr     # whole library, OpenAI OCR for bad pages
scripts\run.ps1 status
```

Each command writes `D:\Readings\librarian\logs\<timestamp>-<command>.log` (DEBUG: every
page's method, quality and line count; every OCR call with token usage) and a
`.summary.json` next to it. `-v` also shows DEBUG on the console.

`--select` takes `priority` (`config/priority.txt`), `all`, or a path substring / sha prefix.
Everything is incremental and cached by SHA-256; `--force` redoes a stage.

## Stages

| Stage | What it does | Output (under the data dir) |
|---|---|---|
| inventory | hash, dedupe, probe text layer, legacy seeds | `books`/`files` tables, `reports/coverage.md` |
| extract | pdftotext per page → quality score → legacy page → nougat (`--nougat`) → LLM OCR (`--llm-ocr`: Codex CLI or OpenAI API, parallel) | `objects/<sha>/pages/NNNN.md`, `extract.json` |
| pagemap | printed page per PDF page from running heads/feet; `pagemaps/` overrides | `pages.printed_page`, `objects/<sha>/book.md` |
| index | page FTS5 + 12-line chunks embedded (bge-base on GPU, or Ollama) in sqlite-vec | `librarian.sqlite` |
| search | FTS + vectors merged by reciprocal rank, definition pages boosted | citations |
| publish | phase 3 | `G:\My Drive\Librarian` |

Line *n* of a citation is line *n* of `objects/<sha>/pages/NNNN.md`. `book.md` is the
whole book with `<!-- page pdf=N printed=X -->` breaks.

## Inventory / book statuses

| status | meaning |
|---|---|
| seeded | legacy `LIB-<sha16>.md` exists (reused only for pages it does better) |
| pending | readable, text layer, not yet extracted |
| pending-ocr | no usable text layer: needs `--nougat` / `--llm-ocr` |
| extracted | extracted; a few low-quality pages (covers, plates) noted in `reason` |
| extracted-partial | more than 5% of pages still below quality |
| indexed | searchable |
| unsupported / error | with the reason |

## Tests

```powershell
.venv\Scripts\python -m pytest
```
