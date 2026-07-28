from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from rag_parse_chunk.chunker import chunk_markdown
from rag_parse_chunk.db import (
    get_engine,
    ingest_batch_docs,
    ingest_batches,
    ingest_docs,
    ingest_manifest,
    staged_nodes,
)
from rag_parse_chunk.parsers import (
    ParseFailure,
    detect_content_type,
    parse_document,
    pdf_quality,
)
from rag_parse_chunk.settings import get_settings
from rag_parse_chunk.sources import SourceConfig, load_sources

logger = logging.getLogger(__name__)

# The ingest_batch_docs statuses this stage retries when a batch is re-run.
# Docs already staged or skipped are done as far as the parse_chunk stage is concerned and
# must not be fetched again, so that re-running a batch is safe.
_RETRYABLE = ("pending", "error")

# doc_url scheme the local_files reader emits, relative to the source's path
_FILE_URL_PREFIX = "file:///"


@dataclass
class ParseBatchReport:
    batch_id: int
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "staged": 0, "skipped_unchanged": 0, "error": 0, "nodes": 0,
            # docs that parsed but yielded almost no text per page — charts,
            # scans, or a parser that could not read the layout
            "low_text": 0,
        }
    )


def unchanged_post_parse(
    ledger_row: dict | None, text_sha: str, parser_version: str
) -> bool:
    """the post-parsing check: same Markdown from the same parser ⇒ the chunks and
    vectors would come out the same, so skip staging. Catches documents
    whose bytes changed but whose text did not — e.g. a regenerated PDF
    with a fresh embedded timestamp.

    the embed stage still writes text_sha and parser_version to the ledger for
    skipped docs; leaving the old values there makes delta report the doc
    as changed on every run.
    """
    return (
        ledger_row is not None
        and ledger_row.get("text_sha") == text_sha
        and ledger_row.get("parser_version") == parser_version
    )


def base_metadata(
    doc: dict, manifest_row: dict | None, cfg: SourceConfig | None
) -> dict:
    """The chunk metadata that is the same for every chunk of one
    document; the chunker adds section_path per chunk. Language is the
    source's configured default for now — detecting de/en per chunk comes
    later.

    This becomes the Qdrant payload, so it carries only what a query needs:
    the identity to filter and delete on, and the fields a citation shows.
    The hashes stay on ingest_batch_docs — no query reads them.
    """
    manifest_row = manifest_row or {}
    extra = manifest_row.get("extra") or {}
    published = manifest_row.get("published_at")
    return {
        "manifest_id": doc["manifest_id"],
        "source_url": manifest_row.get("doc_url"),
        "title": manifest_row.get("title"),
        "doc_type": extra.get("doc_type") or (cfg.doc_type if cfg else None),
        "language": extra.get("default_language")
        or (cfg.default_language if cfg else None),
        "published_at": published.isoformat() if published else None,
    }


def _fetch(
    client: httpx.Client, url: str, cfg: SourceConfig | None
) -> tuple[bytes, str | None]:
    """Raw bytes for one document, plus the served content type if there is one.

    - file:/// — a local_files doc_url, which holds the file's path relative
      to the source's configured directory (see the discover reader): read
      the file under cfg.path. There is no served content type, so the
      caller works it out from the %PDF bytes and the .pdf suffix.
    - everything else — HTTP(S) via httpx.
    """
    if url.startswith(_FILE_URL_PREFIX):
        if cfg is None or not cfg.path:
            raise ParseFailure(
                f"{url} needs a local_files source with a configured path"
            )
        # exact inverse of the reader's f"file:///{rel}" — it does not
        # percent-encode, so this must not unquote
        rel = url[len(_FILE_URL_PREFIX):]
        return (Path(cfg.path) / rel).read_bytes(), None
    else:
        response = client.get(url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type")


def _ledger_for_batch(conn, manifest_ids: set[int]) -> dict[int, dict]:
    """The live ledger row for each of these batch sightings, keyed by the
    batch's manifest_id.

    A batch doc is a fresh sighting; its ledger row (if any) is keyed by the
    older sighting whose points are live. Join batch sighting → document →
    live sighting → ledger, filtering on the batch manifest_id (a PK lookup).
    Returns text_sha/parser_version/status — what the post-parsing check needs.
    """
    bm = ingest_manifest.alias("bm")  # this batch's sightings
    lm = ingest_manifest.alias("lm")  # the ledger's live sightings
    return {
        r["manifest_id"]: dict(r)
        for r in conn.execute(
            select(
                bm.c.manifest_id,
                ingest_docs.c.text_sha,
                ingest_docs.c.parser_version,
                ingest_docs.c.status,
            )
            .select_from(bm)
            .join(
                lm,
                (lm.c.source_system == bm.c.source_system)
                & (lm.c.doc_url == bm.c.doc_url),
            )
            .join(ingest_docs, ingest_docs.c.manifest_id == lm.c.manifest_id)
            .where(bm.c.manifest_id.in_(manifest_ids))
        ).mappings()
    }


def run_parse_batch(
    config_path: str | Path,
    batch_id: int,
    engine: Engine | None = None,
    client: httpx.Client | None = None,
) -> ParseBatchReport:
    """The parse_chunk stage for one planned batch (the mapped Airflow task).

    Per doc: fetch the raw bytes from the source (this stage is where
    content is downloaded — orchestration), parse to Markdown, drop docs whose
    text did not change (the post-parsing check), chunk, and write the chunks to
    staged_nodes. A failed doc is recorded with status='error' and the
    batch keeps going; re-running the batch retries only pending and error
    docs. Returns counts only.
    """
    settings = get_settings()
    sources = load_sources(config_path)
    engine = engine or get_engine()
    own_client = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )

    with engine.connect() as conn:
        batch = conn.execute(
            select(ingest_batches).where(ingest_batches.c.batch_id == batch_id)
        ).mappings().first()
        if batch is None:
            raise ValueError(f"unknown batch {batch_id!r} — did plan run?")
        batch_docs = [
            dict(r)
            for r in conn.execute(
                select(ingest_batch_docs).where(
                    ingest_batch_docs.c.batch_id == batch_id
                )
            ).mappings()
        ]
        # The sighting each batch doc points at: it carries the URL to fetch,
        # the source_id, and the fields that become chunk metadata.
        batch_doc_ids = {d["manifest_id"] for d in batch_docs}
        manifest_docs = {
            m["manifest_id"]: dict(m)
            for m in conn.execute(
                select(ingest_manifest).where(
                    ingest_manifest.c.manifest_id.in_(batch_doc_ids)
                )
            ).mappings()
        }
        # The ledger row for each batch doc, keyed by the batch's manifest_id.
        # The ledger is per DOCUMENT, keyed by whichever sighting is live in
        # Qdrant — an older manifest_id than this batch's. Bridge the two in
        # Postgres: batch sighting → document (source_system, doc_url) → live
        # sighting → ledger. Filtering on the batch manifest_id (a PK lookup)
        # beats a row-value IN on (source_system, doc_url).
        ledger_docs = _ledger_for_batch(conn, batch_doc_ids)

    report = ParseBatchReport(batch_id=batch_id)
    try:
        for doc in batch_docs:
            if doc["status"] not in _RETRYABLE:
                continue
            manifest_row = manifest_docs.get(doc["manifest_id"])
            if manifest_row is None:
                raise ValueError(
                    f"batch doc {doc['manifest_id']} has no manifest sighting"
                )
            outcome = _process_doc(
                doc,
                manifest_row,
                ledger_docs.get(doc["manifest_id"]),
                sources.get(manifest_row["source_id"]),
                settings,
                client,
                engine,
                batch_id,
            )
            report.counts[outcome["status"]] += 1
            report.counts["nodes"] += outcome.get("node_count") or 0
            if (outcome.get("parse_quality") or {}).get("low_text"):
                report.counts["low_text"] += 1
    finally:
        if own_client:
            client.close()

    # Nothing is written to ingest_batches: how far a batch has got is read
    # from its docs' ingest_batch_docs.status, which _process_doc updates.
    # A batch counts as parsed when none of its docs is still 'pending'.
    logger.info("parse %s: %s", batch_id, report.counts)
    return report


def _process_doc(
    doc: dict,
    manifest_row: dict | None,
    ledger_row: dict | None,
    cfg: SourceConfig | None,
    settings,
    client: httpx.Client,
    engine: Engine,
    batch_id: int,
) -> dict:
    """Fetch, parse, run the post-parsing check, chunk and write one document's
    chunks; then write its row in ingest_batch_docs. Returns the columns
    that were written."""
    url = manifest_row["doc_url"]
    try:
        raw, header_ct = _fetch(client, url, cfg)
        content_sha = hashlib.sha256(raw).hexdigest()
        content_type = detect_content_type(header_ct, url, raw)
        markdown, parser_version = parse_document(
            raw, content_type, cfg.pdf_parser if cfg else None
        )
        text_sha = hashlib.sha256(markdown.encode()).hexdigest()
        quality = {"markdown_chars": len(markdown)}
        if content_type == "application/pdf":
            quality.update(pdf_quality(raw))
    # OSError covers the file:/// branch (missing file, permissions)
    except (httpx.HTTPError, OSError, ParseFailure) as exc:
        logger.warning("parse error for %s: %s", url, exc)
        outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        _write_outcome(engine, doc, outcome)
        return outcome

    if unchanged_post_parse(ledger_row, text_sha, parser_version):
        outcome = {
            "status": "skipped_unchanged",
            "error": None,
            "content_sha": content_sha,
            "text_sha": text_sha,
            "parser_version": parser_version,
            "node_count": 0,
            "parse_quality": quality,
        }
        _write_outcome(engine, doc, outcome)
        return outcome

    # the hashes stay out of the metadata: they live on ingest_batch_docs and
    # no query reads them from the Qdrant payload
    meta = base_metadata(doc, manifest_row, cfg)
    chunks = chunk_markdown(
        markdown, meta, settings.chunk_size, settings.chunk_overlap
    )
    node_rows = [
        {
            "manifest_id": doc["manifest_id"],
            "node_seq": c["node_seq"],
            "batch_id": batch_id,
            "text": c["text"],
            "metadata": c["metadata"],
        }
        for c in chunks
    ]
    outcome = {
        "status": "staged",
        "error": None,
        "content_sha": content_sha,
        "text_sha": text_sha,
        "parser_version": parser_version,
        "node_count": len(node_rows),
        "parse_quality": quality,
    }
    with engine.begin() as conn:
        # Delete first, then insert. If the re-parse produced fewer chunks
        # than last time, the leftover rows must not stay behind.
        conn.execute(
            delete(staged_nodes).where(
                staged_nodes.c.manifest_id == doc["manifest_id"]
            )
        )
        if node_rows:
            conn.execute(insert(staged_nodes), node_rows)
        _write_outcome_conn(conn, doc, outcome)
    return outcome


def _write_outcome(engine: Engine, doc: dict, outcome: dict) -> None:
    with engine.begin() as conn:
        _write_outcome_conn(conn, doc, outcome)


def _write_outcome_conn(conn, doc: dict, outcome: dict) -> None:
    # Both PK columns: a doc has one row per batch it was planned into, and
    # only this batch's row may move (see 003_plan.sql).
    conn.execute(
        update(ingest_batch_docs)
        .where(
            ingest_batch_docs.c.batch_id == doc["batch_id"],
            ingest_batch_docs.c.manifest_id == doc["manifest_id"],
        )
        .values(**outcome)
    )
