# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Stage 4 (PARSE+CHUNK) of the six-stage ingestion skeleton — `../ARCHITECTURE.md` blocks 2 (parsing), 3 (chunking), 4 (metadata/change detection) and 11 are authoritative. Standalone stage module: `settings.py`, `sources.py`, `db.py` are **deliberately duplicated** from the other stage packages — fix infra bugs in every copy.

## Commands

```bash
uv sync --frozen --extra dev                    # lock-exact venv
.venv/bin/python -m pytest                      # offline tests (no network, no DB)
python -m rag_parse_chunk --batch-id <id>       # parse one batch (needs ../postgres up; fetches live URLs)
```

## Invariants

- **Library-first, thin `__main__`**: stage logic in `rag_parse_chunk.parse_chunk.run_parse_batch()`; `__main__.py` is a logic-free argparse wrapper.
- **This is the only stage that downloads document content** (block 11: fetched live per batch, after delta discarded the ~95 % unchanged). Raw bytes never enter Postgres; chunk text enters only the transient `staged_nodes`.
- **Auth is per request, never on the client** (`_auth_headers`): one batch mixes sources, so Confluence credentials must not ride along on a public ECB fetch. Only `type: confluence` sources get a header today.
- **Confluence fetches go through the API, not the doc_url** — both kinds, keyed on `extra["confluence_kind"]`, because both doc_urls are browser routes:
  - `page` — the permalink serves the whole Confluence UI, so the body comes from `/wiki/api/v2/pages/{id}?body-format=export_view`.
  - `attachment` — **deferred, so none reaches this stage today** (`../ARCHITECTURE.md` block 1; discover lists pages only). The branch is kept and tested: `/wiki/download/attachments/...` only accepts a browser session and answers Basic auth with a 401 HTML error page, which then reached the parsers as text/html (measured 2026-07-30), so the bytes come from `/wiki/rest/api/content/{page_id}/child/attachment/{attachment_id}/download`, the path the v2 attachment's own `downloadLink` names; the served content type then routes them into the existing PDF parser. `parse_confluence` uses **markdownify, not trafilatura**: trafilatura dropped every heading and discarded short pages on a body fragment (measured 2026-07-29; `../ARCHITECTURE.md` stage 4 records it).
- **One normalized intermediate: Markdown** (block 2). New format = new parser in `PARSER_REGISTRY` keyed by content type; `parser_version` must embed the library version so upgrades reprocess old docs (block 4) — for PDFs that means pymupdf4llm **and** pymupdf-layout, the engine that decides the output. PDF OCR stays off (`use_ocr=False`) until scanned sources arrive (needs tesseract in the image + the OCR cost class in stage 3). pymupdf4llm output goes through `_strip_parser_noise` before chunking: inline `<mark>`/`<u>` and picture-text markers out, their text and table-cell `<br>` kept; heading lines also lose trailing spaces and bold markers, because the chunker turns heading text into `section_path` (`# **Titel** ` ⇒ `/Titel /`) and that value reaches the chunk metadata and the citation. Body-text emphasis stays.
- **Layer-2 rule** (`unchanged_post_parse`): same `text_sha` + same `parser_version` vs the ledger ⇒ `skipped_unchanged`, no staging. Stage 5 must still refresh ledger signals for skipped docs, or delta re-triggers 'changed' forever. Stage 4 only READS `ingest_docs` — ledger writes are stage 5's exclusively.
- **PDF parser is per source, layout by default**: `PDF_PARSERS` holds `layout` (the pymupdf-layout engine) and `fast` (same library, engine off); a source picks with `pdf_parser:` in `sources.yaml`, and the field must exist in **every** copy of `sources.py` or the other stages reject the config (`extra="forbid"`). The two modes carry different `parser_version` strings, so switching a source reprocesses its documents. Measured 2026-07-22: `fast` is under 2× quicker and loses most headings and tables — no source uses it.
- **Parse quality is recorded, not acted on**: `parse_quality` jsonb on `ingest_batch_docs` (pages, chars_per_page, image_coverage, pages_low_text, low_text) so a document that parses "successfully" but yields almost nothing stops being silent; the batch report counts `low_text`. Measured off the PDF, not the Markdown, so it stays comparable across parsers. Vector charts don't register as images — they show up as low text.
- **Per-doc failure isolation**: fetch/parse errors → `status='error'` + detail on `ingest_batch_docs`, batch continues; batch re-runs retry only `pending`/`error` docs. Re-parse deletes a doc's staged rows before insert (no stale tails).
- **An empty document is not an error**: no text at all (empty Confluence page, scanned PDF with OCR off) raises `parsers.EmptyDocument` — a `ParseFailure` subclass, so it must be caught *before* the generic clause — and is recorded as `skipped_empty` with `text_sha=EMPTY_TEXT_SHA`, no staged rows. `error` would leave it without a ledger row, so delta re-planned it every run and the finalize gate counted it as a failure. Deliberately not `skipped_unchanged`: that tells stage 5 to keep the vectors, `skipped_empty` tells it to delete them.
- **`base_metadata` is the Qdrant payload**, verbatim (plus `section_path` per chunk from the chunker, and `text`/`node_seq` from stage 5). Postgres is not in the query path, so anything the retrieval stage filters, cites or displays has to be here: `manifest_id`, `source_system`, `source_id`, `source_url`, `title`, `doc_type`, `language`, `published_at`. Hashes stay on `ingest_batch_docs`. Adding a field only reaches existing points after a re-ingest, and if it is filtered on it also needs an entry in `../embed/src/rag_embed/qdrant.py:PAYLOAD_INDEXES`.
- **Chunking is offline-deterministic**: explicit whitespace tokenizer (sizes ≈ words) — never let the splitter fall back to tiktoken (network vocab download in batch containers/tests).
- Schema: `../postgres/sql/004_parse_chunk.sql` is authoritative for `staged_nodes`; `db.py` mirrors it (plus manifest/ledger/batch mirrors) — change together.
