# Ingesting new books (design)

Status: proposal, 2026-11-05. Nothing here is built yet.

Goal: books that turn up anywhere (Google Drive, the Windows Downloads folder, later other
folders) reach the library without duplicating what is already there, get OCR'd by Codex in the
library's processing order, are classified from their own table of contents and front matter,
and are filed, tagged and indexed. One command, resumable, with a status file, and a person
asked only about the genuinely uncertain cases.

## Principles

* **Originals are never touched.** Candidates are copied into a staging folder and the copy is
  verified by SHA-256. Drive files are never moved (a move would sync as a deletion). Cleaning up
  Downloads is a separate, explicit step.
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
discovered -> staged -> identified -> profiled -> classified -> [review] -> filed -> ocr -> indexed
                           |                                        |
                           +-> duplicate / not-a-book / rejected    +-> rejected (remembered)
```

### 1. Discover

`config/local.toml` gains inbox roots, e.g.

```toml
inbox = ['C:\Users\william\Downloads', 'G:\My Drive\Books']
inbox_exclude = ['G:\My Drive\Librarian']        # the published index
```

* Files: `.pdf .epub .djvu .djv`. Skip partial downloads (`.crdownload`, `.part`, size changed
  between two looks a minute apart), files under 100 KB, and anything under Sources or the
  publish folder.
* Drive for desktop keeps most files online-only; reading one downloads it. Discovery lists
  names and sizes only (no download); staging hydrates them in small batches.

### 2. Stage

Copy to `D:\Readings\Inbox\<sha256>.<ext>`, hash while copying, verify. Exact-hash hits against
the library, an earlier candidate or the reject list stop here (state `duplicate`, no copy kept).

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
  complete vs missing pages). Per the library rule, kept, but as a weaker copy: added to
  `config/lower-priority.txt`, end of every queue.
* **duplicate, better**: same edition, the new copy is better (e.g. born-digital vs our scan).
  The new copy takes the plain file name; ours is renamed ` - 2` and goes to lower priority.
  Its Lean check reports and subject tags carry over (same work).
* **new edition**: same work, other edition. The newer edition is the primary copy, the older
  one is renamed ` - 1st ed` (as done for Deitmar and Shonkwiler) and goes to lower priority.
* **uncertain**: conflicting signals (title matches but text does not, or ISBN missing and
  similarity in between). Goes to review.
* **not a book**: a paper under ~30 pages, slides, a non-maths PDF (Downloads is full of
  these). Decided in step 5 from the profile; remembered.

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
`new_subject`), plus `is_math_book`. Low confidence or a `new_subject` goes to review; the rest
is accepted.

### 6. Review (only when needed)

`reports/intake-review.csv`, one row per open question: the verdict or tag proposed, the
evidence (matching book, similarity, ISBNs, editions), and an `answer` column (`ok`, `reject`,
`new`, `dup`, or a corrected subject). `librarian intake apply` reads it back. With nothing
uncertain there is no sheet.

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
librarian intake scan            # discover + stage + identify + profile + classify; writes the review sheet
librarian intake apply           # read answers from intake-review.csv
librarian intake file            # move accepted books into Sources, tags, lower-priority, inventory
librarian intake queue           # write config/ocr-queue-intake.txt in processing order
scripts\ocr_batch.py --queue config/ocr-queue-intake.txt --all-pages
librarian intake status          # counts by state; also reports/intake-status.md
librarian intake cleanup         # optional: list (then delete, with confirmation) Downloads originals already filed
```

`scan` is incremental: it only looks at files it has not seen (path + size + mtime), so it can run
as a nightly scheduled task ahead of the night OCR batch.

## Costs

* Identification OCR: ~5 Codex pages per scanned candidate; none for text-layer files.
* Front matter: ~3–8% of a scanned book, done once and reused by the full OCR.
* Classification: one Codex call per book.
* Full OCR: as today (~500 pages/hour at 3 workers).

## To decide

1. Which Drive folders are inboxes (all of My Drive, or specific folders)?
2. After a book is filed, what happens to the Downloads original: leave it, or delete it after
   `intake cleanup` confirms?
3. Are papers (under ~30 pages) wanted in the library, or rejected as "not a book"?
4. Auto-accept high-confidence classifications, or review every new book's subject once?
5. Keep a duplicate that is worse in every respect, or reject it? (Current rule: keep, last in
   the queues.)
