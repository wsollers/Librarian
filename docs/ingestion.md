# Ingesting new books (design)

Status: design agreed 2026-11-05 (decisions at the end). Nothing here is built yet.

Goal: books that turn up anywhere (Google Drive, the Windows Downloads folder, later other
folders) reach the library without duplicating what is already there, get OCR'd by Codex in the
library's processing order, are classified from their own table of contents and front matter,
and are filed, tagged and indexed. One command, resumable, with a status file, and a person
asked only about the genuinely uncertain cases.

## Principles

* **Every inbox file gets a disposition, then the original is deleted.** A duplicate is deleted
  as soon as it is identified. Anything else is copied into staging (verified by SHA-256) and the
  original is deleted only once the book is filed in the Readings tree, or rejected. Nothing is
  deleted before the library holds a verified copy or the file is known to be unwanted. Deleting
  on Drive goes to Drive's trash (kept 30 days), so a mistake there is recoverable for a month.
* **SHA-256 is identity.** Everything downstream (pages, OCR cache, vectors, subjects, queues)
  is already keyed by it, so a candidate is tracked by its hash from the moment it is read.
* **Every decision is remembered.** A file rejected once (not maths, a duplicate, "no") is never
  asked about again, wherever it reappears.
* **Codex does all the reading**: OCR, and the classification proposal from TOC + front matter.
* **Same machinery as today**: inventory, `extract --llm-ocr` via `scripts/ocr_batch.py`, the
  subject tags in `config/subjects.yaml`, the move log + relink, `index`.

## Pipeline

Each candidate moves through these states, stored in a new `intake` table in librarian.sqlite
(sha256, first path seen, every path seen, size, state, verdict, reasons, timestamps):

```
discovered -> staged -> identified -> profiled -> classified -> filed -> ocr -> indexed
                           |                                        |
                           +-> duplicate / not-a-book / rejected    +-> rejected (remembered)
```

### 1. Discover

`config/local.toml` gains inbox roots, e.g.

```toml
inbox = ['C:\Users\william\Downloads',
         'G:\My Drive\Saved from Chrome',        # where the phone's Chrome saves downloads
         'G:\My Drive\Input']                    # anything from the phone that lands elsewhere
inbox_exclude = ['G:\My Drive\Librarian']        # the published index
```

(Folder names to confirm on hal5000 once `Input` exists.)

* Files: `.pdf .epub .djvu .djv`. Skip partial downloads (`.crdownload`, `.part`, size changed
  between two looks a minute apart), files under 100 KB, and anything under Sources or the
  publish folder.
* Drive for desktop keeps most files online-only; reading one downloads it. Discovery lists
  names and sizes only (no download); staging hydrates them in small batches.

### 2. Hash first, then stage

Cheapest checks first:

1. **Size + full SHA-256** of the original (one sequential read). A hash already in the library,
   among earlier candidates or on the reject list is an exact duplicate: the original is
   **deleted at once** (logged with its hash and path), nothing is staged.
2. Otherwise copy to `D:\Readings\Inbox\<sha256>.<ext>`, verify the copy's hash, and continue
   with the costlier checks below on the staged copy.

### 3. Identify: is it a book we already have?

Byte-identical copies are rare; the usual duplicate is the same book from another source (a
re-saved PDF, a different scan, another edition). Evidence, cheapest first:

| Signal | How | Strength |
|---|---|---|
| ISBN-10/13 (checksum-validated), DOI | regex over the copyright page and the PDF metadata | decisive for the same edition; an ISBN on an edition we have is a duplicate |
| Edition and year | "Second Edition", "© 2009", "1st edition: ... 2nd edition: ..." on the copyright page | separates editions of one work |
| Text fingerprint | MinHash over word 5-shingles of ~20 body pages, compared with a fingerprint per library book (built once from `objects/<sha>/pages`, ~900 books: instant) | Jaccard > 0.6: same work; > 0.85: same edition |
| Title / author | PDF metadata, title page, file name, fuzzy-matched against `books` | a hint only, never decides |

A scan with no text layer first gets a small **identification OCR**: the title page, the copyright
page and three body pages through Codex (~5 pages). Those pages go into the OCR cache, so they
are not paid for again later.

Verdicts:

* **new**: nothing matches.
* **duplicate, worse**: same edition, our copy is better (text layer vs scan, OCR quality,
  complete vs missing pages). **Deleted** (staged copy and original). Incoming worse copies are
  not added; the keep-both rule applies only to copies already in the library.
* **duplicate, better**: same edition, the new copy is better (e.g. born-digital vs our scan).
  The new copy takes the plain file name; ours is renamed ` - 2` and goes to lower priority.
  Its Lean check reports and subject tags carry over (same work).
* **new edition**: same work, other edition. The newer edition is the primary copy, the older
  one is renamed ` - 1st ed` (as done for Deitmar and Shonkwiler) and goes to lower priority.
* **uncertain**: conflicting signals (title matches but text does not, or ISBN missing and
  similarity in between). Held in staging and listed in the run summary until decided.
* **paper**: at most 125 pages. Moved to `D:\Readings\Papers` (copied, verified, original
  deleted) for William to sort by hand; a copy turning up again is a duplicate.
* **not maths or science**: programming, security, manuals and the rest of Downloads. Moved to
  `D:\Readings\Hold` by the triage step.

### 4. Profile: TOC and front matter

* Text-layer PDFs and EPUBs: read directly (EPUB nav gives the TOC; DjVu goes through
  `djvu2pdf` first).
* Scans: **OCR the front matter in full** (up to the first chapter, at most ~8% of the book)
  with Codex. Unlike `--body-only` for the existing library, intake needs the TOC and preface.
* `profiles.py` already builds TOC + preface/introduction and the dossier used for subject
  tagging; it runs on the staged copy.

### 5. Classify

Codex gets the dossier (title, author, TOC, preface, a few body lines) plus `config/taxonomy.yaml`
and the instructions used for the current tags, and returns the same record as
`config/subjects.yaml` (primary, up to 3 secondary, level, kind, confidence, note, optional
`new_subject`), plus `is_math_book`. Low confidence or a `new_subject` is filed under the best
existing subject and flagged in the summary; the rest is accepted.

### 6. Summary, and corrections through the librarian MCP

High-confidence results are accepted and filed without asking. Each run ends with a short
summary (`reports/intake-summary.md`, and in chat when asked): books added with their subject
folder, duplicates deleted, new editions, and the few items that need a decision (an uncertain
duplicate, a low-confidence or new subject). Those are settled in conversation through new
librarian MCP tools, so a book or two in the wrong place is a one-line fix:

| Tool | Does |
|---|---|
| `intake_summary` | the last run's summary and anything still waiting for a decision |
| `intake_decide(sha, verdict)` | settle an uncertain one: `new`, `duplicate` (deleted), `edition` |
| `move_book(sha, subject)` | refile a book (any book, not only new ones): moves the file, updates its tag, logs the move, relinks the lists |
| `tag_book(sha, primary, secondary)` | change subject tags without moving |
| `add_paper(path_or_url)` | take in a paper by hand: staged, OCR'd, classified like a book (kind `paper`) |

The MCP server is read-only today; these are its first write tools, each logged.

### 7. File

* Name: `Title - Author.ext` from the title page/metadata, the library convention; a clash
  gets ` - 2` or an edition suffix.
* Move from staging into `Sources/<Area>/<Subject>/`, logged like `move-to-subjects.ps1`.
* Tags appended to `config/subjects.yaml`; weaker copies appended to `lower-priority.txt`.
* `inventory` adopts the file under its known hash (nothing is re-read).

### 8. Queue, OCR, index

`librarian intake queue` writes `config/ocr-queue-intake.txt` in the agreed order instead of a
hand-made list:

1. Foundations, by subject: Type Theory, Mathematical Logic, Set Theory, Number Systems, then the
   rest of Foundations; then Analysis; then the other areas.
2. Within a subject: fewest pages needing OCR first (most books finished per batch).
3. Physics, Calculus and Precalculus after all of that (excluded from OCR until decided otherwise,
   as now).
4. Weaker copies last.

`scripts/ocr_batch.py --queue config/ocr-queue-intake.txt --all-pages` then does OCR, page map,
sections and index one book at a time with its status file. Finally the book is appended to the
Lean check order (`scripts/make_leancheck_list.py`).

## Commands

```
librarian intake scan            # discover, hash (deleting exact duplicates), stage, identify, profile, classify
librarian intake file            # move accepted books into Sources, tags, inventory; delete their originals
librarian intake queue           # write config/ocr-queue-intake.txt in processing order
scripts\ocr_batch.py --queue config/ocr-queue-intake.txt --all-pages
librarian intake status          # counts by state; also reports/intake-summary.md
```

`scan` is incremental: it only looks at files it has not seen (path + size + mtime), so it can run
as a nightly scheduled task ahead of the night OCR batch.

## Costs

* Identification OCR: ~5 Codex pages per scanned candidate; none for text-layer files.
* Front matter: ~3–8% of a scanned book, done once and reused by the full OCR.
* Classification: one Codex call per book.
* Full OCR: as today (~500 pages/hour at 3 workers).

## Decisions (2026-11-05)

1. **Inboxes:** Downloads, Drive `Saved from Chrome` (phone downloads) and a new Drive `Input`
   folder for anything from the phone that lands elsewhere.
2. **Originals:** every inbox book is deleted after its disposition. Exact duplicates are found
   with the cheap full-file hash first and deleted immediately; the costlier checks run only on
   what is left. A new book's original is deleted once it is filed in the Readings tree.
3. **Papers:** not ingested automatically; added by hand through the librarian MCP.
4. **Review:** no sheet. A summary after each run; the odd misfiled book is corrected through
   librarian MCP tools (to be added).
5. **Incoming duplicates that are worse in every respect:** deleted.

Added after the first check (2,010 inbox files; 613 exact duplicates, 26 other editions):

6. **Other editions** (newer or older): kept, and processed after everything else (end of the
   OCR and Lean check queues).
7. **Papers and notes:** anything up to **125 pages** is a paper: moved to `D:\Readings\Papers`
   for sorting by hand (a tight set of notes can be worth keeping; that is taste, not a rule).
   `librarian intake sort --apply` does this together with deleting exact duplicates.
8. **Not maths or science** (programming, security, ...): moved to `D:\Readings\Hold`. This is
   decided by the triage step (Codex), not by a rule.
