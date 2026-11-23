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

## Network search: Windows host and Linux clients

The read-only REST API and HTTP MCP endpoint are implemented. Follow the
[WinSW setup guide](docs/windows-services.md) for credentials, local HTTPS certificates, Windows Firewall,
Linux REST/Codex configuration, and automatic startup. The configured server is `192.168.1.243` and the allowed client network is `192.168.1.0/24`; no VPN, cloud relay, or public tunnel is used.

The backend binds only to `127.0.0.1:8765`. Caddy serves HTTPS on the selected LAN IP;
Windows Firewall and Caddy restrict TCP 443 to `192.168.1.0/24`. **Current mode trusts the LAN:
no token, environment variable, or OAuth login is required.** The API additionally checks the
forwarded client address from its loopback proxy. Operations remain read-only.

The public CA is tracked at [`config/certs/librarian-root.crt`](config/certs/librarian-root.crt).
On Debian/Ubuntu, from the repository root:

```bash
sudo install -m 644 config/certs/librarian-root.crt /usr/local/share/ca-certificates/librarian-root.crt
sudo update-ca-certificates
curl https://hal5000.fios-router.home/v1/status
```

In `~/.codex/config.toml`, use this entry and remove any previous `bearer_token_env_var` line:

```toml
[mcp_servers.librarian]
url = "https://hal5000.fios-router.home/mcp"
tool_timeout_sec = 75
```

Restart Codex after editing its configuration. See the [WinSW setup guide](docs/windows-services.md)
for installing/updating the server. Bearer-token mode remains available as an explicit option
for later; existing tokens are preserved but unused in LAN-trust mode.

| Endpoint | Purpose |
|---|---|
| `POST /v1/search` | Existing filters; query, k (1–50), mode (hybrid/phrase/fts/semantic), rerank |
| `GET /v1/books` | filter, author, subject, limit (1–100), offset |
| `GET /v1/books/{id}/pages/{page}` | Line-numbered text; optional line_start, line_end, context (0–50) |
| `GET /v1/books/{id}/contents` | Contents; optional depth (1–3) |
| `GET /v1/subjects` | Subject filters |
| `GET /v1/status` | Readiness and state: loading, ready, failed |
| `/mcp` | search, read_page, list_books, contents, subjects, status |

Book IDs are full SHA-256 strings; page numbers are PDF pages. Hits preserve printed-page and
line citations and return HTTPS `page_url` links. PDF downloads and query expansion are not
exposed. The local stdio MCP server also has management tools; HTTP never registers them.

REST and MCP share one model instance and search lock. Request bodies are limited to 16 KiB,
queries to 2,048 characters, and operation responses to 1 MiB. At most four operations run or
wait for the search lock; excess calls receive 429. Operations return 504 after 60 seconds,
but unfinished GPU/SQLite work retains its slot until it completes. Body uploads have a
10-second deadline. Search returns 503 during startup; poll status until ready. MCP reports
errors in tool result objects. Host/Origin checks, sanitized errors, and disabled HTTP access
logs protect the boundary. Service telemetry is written to gitignored `.runtime/logs`, with
90-day retention enforced at startup and after every HTTP request (including rejected requests).
Logs record client/IP, request ID, route, status, duration, and operation hit counts; never tokens,
query text, or book text. Restart after indexing to refresh model-side caches.

Keep any future live search UI on the LAN service. A hosted Sites backend cannot directly
reach it; adding a tunnel would change this boundary. Spaces can hold deliberately saved
research and citations. Text returned to Codex may be sent to its model provider as part of
the conversation, even though search runs locally.

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
