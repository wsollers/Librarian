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
notepad .env                           # optional settings (see .env.example)
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
scripts\run.ps1 audit                         # reports\audit.md: what still isn't indexed correctly, and why
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
| extract | pdftotext per page → quality score → Codex OCR for low pages (`--llm-ocr`, parallel; without it, legacy pages fill in) | `objects/<sha>/pages/NNNN.md`, `extract.json` |
| pagemap | printed page per PDF page from running heads/feet; `pagemaps/` overrides; chapters/sections from the contents page (fallback: numbered headings) | `pages.printed_page`, `sections`, `objects/<sha>/book.md` |
| index | page FTS5 + 12-line chunks embedded (bge-base on GPU, or Ollama) in sqlite-vec | `librarian.sqlite` |
| search | FTS + vectors merged by reciprocal rank, definition pages boosted | citations |
| publish | phase 3 | `G:\My Drive\Librarian` |

Every hit carries book, author, chapter › section, printed page, PDF page, line span and a
`file:///…pdf#page=N` link. Line *n* of a citation is line *n* of `objects/<sha>/pages/NNNN.md`. `book.md` is the
whole book with `<!-- page pdf=N printed=X -->` breaks.

## Search filters

```powershell
scripts\run.ps1 search "compact" --author Kainth --chapter Compactness
scripts\run.ps1 search "Lipschitz" --book "Analysis I" --section 5.4
scripts\run.ps1 search "lipsctiz constant"     # -> searched for: lipschitz constant
```

`--author` / `--book` narrow to books, `--chapter` / `--section` (number or title words) to page
ranges from the parsed contents; inside a scope vectors are compared exactly (no KNN recall loss).
Misspelled words are corrected against the library's own vocabulary (`--no-spell` to turn off;
word list cached in `<data>\cache\vocab.json`, rebuilt when the index changes).

## MCP server (Claude desktop app)

```powershell
scripts\bootstrap.ps1        # installs the MCP SDK into .venv if missing
scripts\install-mcp.ps1      # registers "librarian" in claude_desktop_config.json (backup kept)
# then quit and restart the Claude desktop app
```

`librarian mcp` is a stdio server the desktop app starts and stops. It answers at once and loads
the index (read-only), bge, the cross-encoder and the word list in the background; tools:
`search` (query, author, book, chapter, section, mode, k, expand, rerank), `read_page`,
`contents`, `list_books`, `status`. Cloud sessions linked to hal5000 reach it too.

Ollama is **not** started with the server. Only `search(expand=true)` needs it: then the server
uses a running Ollama, or starts `ollama serve` itself (in a Windows Job Object, so it dies with
the server), and loads `LIBRARIAN_EXPAND_MODEL` with `keep_alive=-1`. On exit it unloads the
model and stops Ollama only if it started it. Log: `<data>\logs\<stamp>-mcp.log`.

## Inventory / book statuses

| status | meaning |
|---|---|
| seeded | legacy `LIB-<sha16>.md` exists (reused only for pages it does better) |
| pending | readable, text layer, not yet extracted |
| pending-ocr | no usable text layer: needs `--llm-ocr` |
| extracted | extracted; a few low-quality pages (covers, plates) noted in `reason` |
| extracted-partial | more than 5% of pages still below quality |
| indexed | searchable |
| unsupported / error | with the reason |

## Tests

```powershell
.venv\Scripts\python -m pytest
```
