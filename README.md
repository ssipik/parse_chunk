# rag-parse-chunk

Stage 4 (PARSE+CHUNK) of the RAG ingestion pipeline — see [`../ARCHITECTURE.md`](../ARCHITECTURE.md) (blocks 2, 3, 11). For one planned batch (a mapped Airflow task later): fetch each document's raw bytes **live from the source** (the first time content is downloaded), normalize to **Markdown with structure preserved**, drop unchanged text (block 4 layer 2), chunk, and stage nodes to Postgres.

- **Parsers** (`parsers.py`): HTML → trafilatura (markdown output, boilerplate stripped, tables kept); PDF → pymupdf4llm with the pymupdf-layout engine (local, column-aware, table detection; OCR off until scanned sources arrive). `parser_version` embeds the library versions — an upgrade reprocesses already-ingested docs on the next run.
- **Layer-2 skip**: `text_sha` of the normalized Markdown vs the ledger — identical text from the same parser ⇒ `skipped_unchanged`, nothing reaches embedding (stage 5 still refreshes the ledger's change signals for such docs).
- **Chunking** (`chunker.py`): `MarkdownNodeParser` → `SentenceSplitter` (~700 words ≈ upper half of the 512–1024-token budget, ~15 % overlap); `section_path` from the heading hierarchy rides on every chunk. An explicit whitespace tokenizer keeps chunking offline and deterministic.
- **Staging**: chunks land in `staged_nodes` (transient — finalize deletes them); per-doc outcome (`staged` / `skipped_unchanged` / `error` + shas, node_count, parse_quality) in `ingest_batch_docs`.
- **Parse quality** (`parse_quality` jsonb): pages, chars_per_page, image_coverage, pages_low_text and a `low_text` flag, read off the PDF rather than the Markdown so it compares across parsers. The batch report counts `low_text` docs — a page whose content is a chart or a scan yields nothing, and used to do so silently.

## Usage

```bash
uv sync --frozen --extra dev        # lock-exact venv from uv.lock
.venv/bin/python -m pytest          # offline tests (parsers, chunker, pure logic)

# Parse one batch (Postgres from ../postgres must be up; fetches live URLs)
python -m rag_parse_chunk --batch-id <id>

# Containerized
docker compose run --rm --build pipeline python -m rag_parse_chunk --batch-id <id>
```

## Design notes

- **Failures are per-doc, never per-batch**: fetch/parse errors set `status='error'` with detail and continue; re-running the batch retries only pending/error docs. finalize's quality gate watches the error rate.
- **Re-parse replaces, never merges**: a doc's staged nodes are deleted before insert so a shrunken re-parse leaves no stale tail.
- Knobs: `RAG_CHUNK_SIZE` (default 700 words), `RAG_CHUNK_OVERLAP` (default 100), `RAG_HTTP_TIMEOUT`, `RAG_HTTP_USER_AGENT`; per source, `pdf_parser: layout | fast` in `sources.yaml` (default `layout`; `fast` drops the layout engine — under 2× quicker, most structure lost, so nothing uses it).
- Standalone module by convention: infrastructure (`settings.py`, `sources.py`, `db.py`) duplicated from the other stages, dedup deferred.
- **PDF parsers are compared in [`eval/`](eval/README.md)**, not by guesswork: it renders each parser's Markdown back to PDF for side-by-side reading. That is how LiteParse was replaced on 2026-07-22 (block 2). Its venv is separate, so it never touches `uv.lock`.
- PyMuPDF is **AGPL-3.0** — fine for internal self-hosted use, a commercial Artifex license if that ever changes. PDF parsing also costs ~3–5 s per document (ONNX layout model), against ~30 ms for the old parser.
