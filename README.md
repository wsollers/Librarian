# Librarian

Library infrastructure for William's mathematics books (~905 files in
`D:\Readings\Sources`). Goal: every book usable as **page-exact, line-numbered
Markdown**, searchable by exact phrase (SQLite FTS5) and by description
(embeddings), with citations of **book, printed page, PDF page and lines**.

Consumers: AdventuresInMathematics (AIM), LRA, and anything else. Background,
decisions and phases are in [HANDOFF.md](HANDOFF.md).

> **Copyright.** Extracted text, indexes and coverage reports live under the
> data directory (default `D:\Readings\librarian`) and are published only to
> private Drive. They are never committed: see `.gitignore`.

## Stages

| Stage | Command | Status |
|---|---|---|
| inventory | `librarian inventory` | done (phase 1) |
| extract | `librarian extract` | phase 2 |
| page map | (part of extract) | phase 2 |
| index | `librarian index` | phase 2 |
| search | `librarian search` | phase 2 |
| publish | `librarian publish` | phase 3 |

All stages are incremental and cached by SHA-256.

## Quick start (hal5000)

```powershell
py -m venv .venv; .venv\Scripts\activate
pip install -e .[manifest,dev]
copy config\example.toml config\local.toml   # adjust paths
librarian inventory          # hashes, dedupes, probes; writes reports\coverage.md
pytest
```

Requires poppler (`pdfinfo`, `pdftotext`) on PATH; `djvutxt` (djvulibre) for DjVu.

## Inventory statuses

| status | meaning |
|---|---|
| seeded | legacy `LIB-<sha16>.md` exists; verified against `pdfinfo` in phase 2 |
| pending | readable PDF with a text layer, not yet extracted |
| pending-ocr | no usable text layer: nougat, then LLM OCR (OpenAI > Claude > Gemini > Copilot; not Claude by default) |
| unsupported | not pdf/djvu/epub |
| error | unreadable, with the reason |

Every distinct book has exactly one status, so the coverage report has no silent gaps.
