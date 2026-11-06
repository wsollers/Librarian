---
name: librarian
description: Search William's mathematics book library (Librarian MCP server) and cite book, section, printed page, PDF page and lines. Use for /librarian, or when asked where a definition, theorem or topic appears in his books.
---

# Librarian

William's library (~900 mathematics books under `D:\Readings\Sources`) is indexed page by page on
hal5000. The `librarian` MCP server (tools `search`, `read_page`, `contents`, `list_books`, `intake_check`,
`status`) answers from that index. Every hit has book, author, chapter › section, printed page,
PDF page, a line span and a `file:///…pdf#page=N` link.

If the librarian tools are missing: say the Librarian server isn't connected; on hal5000 it is
registered with `scripts\install-mcp.ps1` in `F:\repos\librarian`, then the Claude desktop app is
restarted. Fallback: `scripts\run.ps1 search "<terms>" --json` in that repo, if a shell on hal5000 is available.

## Commands

* `/librarian status` → call `status`; report ready/error, books and pages indexed, last indexed,
  GPU, and whether Ollama is running. There is no start step: the desktop app starts the server,
  and Ollama is started only by a search with `expand=true`.
* `/librarian search [author:X] [book:Y] [chapter:Z] [section:S] <terms or question>` → the workflow below.
  Words before the terms that name an author or a book can be treated as filters even without a prefix
  when that is clearly meant ("/librarian search Tao compactness").
* `/librarian contents <book>` → `contents` (depth 2), shown as an outline with printed pages.
* `/librarian books [subject or author]` → `list_books`.
* `/librarian read <book> p. <printed>` (or `PDF <n>`) → `read_page`.
* `/librarian check ingest` → `intake_check(run=true)`: starts a fresh check of the inbox folders
  (Downloads, Drive `Saved from Chrome` and `Input`) in the background and returns the last result.
  Say it has started, then summarise the last result if there is one: counts by verdict, the new
  books (title, author, pages, scan or text), and possible duplicates or other editions with the
  library book they resemble. Call `intake_check()` again (no `run`) when asked for the result or
  after a few minutes; while `running` is present it is not finished. Papers and exact duplicates
  are counts only; never list them unless asked. The check never deletes or moves anything;
  `scripts\run.ps1 intake sort --apply` on hal5000 deletes exact duplicates and moves papers
  (at most 125 pages) to `D:\Readings\Papers`.

## Search workflow

1. **Turn the question into textbook terms yourself.** For a description ("continuity where the
   change is bounded by a constant") search with the standard names ("Lipschitz continuity",
   "Lipschitz constant") — run 2–3 searches with different phrasings or notation if the first is
   thin. Keep the user's own wording as one of the searches; the cross-encoder reranks against it.
   Use `expand=true` only if you have no idea of the term (it loads a local model: slow first time).
2. **Filter when the question names a scope**: `author`, `book` (title substring or sha256),
   `chapter` / `section` (number like `5.4` or title words). `mode="phrase"` for an exact quotation.
   If `corrected` appears in the result, the server fixed a misspelling: mention what was searched.
   If `scope.note` says nothing matched, check the name with `list_books` or `contents`.
3. **Read before asserting.** For each hit you will rely on, call `read_page` with its `sha256`,
   `pdf_page` and `lines` (context 6–10). Snippets are cut and can be OCR text; quote only what
   `read_page` shows. If the statement continues, read `pdf_page + 1`. If `text_source` is an OCR
   method or `quality` is low, say the text is OCR and may contain errors in formulas.
4. **Prefer definitions and theorems** (hits tagged `def`) over passing mentions; group results by
   book; give two or three strong sources rather than ten weak ones unless asked for a survey.

## Citing

One line per source:

> *Title* — Author, chapter › section, p. <printed> (PDF <pdf>), lines a–b — [open](pdf link)

Quote the definition or theorem as read (formulas in LaTeX when the page text is clear), then the
citation. If the printed page is unknown, give the PDF page only.

## Proofs

William prefers to attempt proofs himself: when a found result is a theorem he is working on,
point to where the proof is (book, page) and give structural hints only, unless he asks for the proof.
