from __future__ import annotations

import base64
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
    CONFLUENCE_CONTENT_TYPE,
    EmptyDocument,
    ParseFailure,
    detect_content_type,
    parse_document,
    pdf_quality,
)
from rag_parse_chunk.settings import get_settings
from rag_config import SourceConfig, load_sources

logger = logging.getLogger(__name__)

# The ingest_batch_docs statuses this stage retries when a batch is re-run.
# Docs already staged or skipped are done as far as the parse_chunk stage is concerned and
# must not be fetched again, so that re-running a batch is safe.
_RETRYABLE = ("pending", "error")

# doc_url scheme the local_files reader emits, relative to the source's path
_FILE_URL_PREFIX = "file:///"

# text_sha of a document with no text. Stored rather than NULL so a page that
# is still empty next run compares equal in unchanged_post_parse.
EMPTY_TEXT_SHA = hashlib.sha256(b"").hexdigest()


@dataclass
class ParseBatchReport:
    batch_id: int
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "staged": 0, "skipped_unchanged": 0, "skipped_empty": 0, "error": 0,
            "nodes": 0,
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
    the identity to filter and delete on, the fields the agent filters on
    (source_system, source_id, doc_type, published_at), and the fields a
    citation shows. The hashes stay on ingest_batch_docs — no query reads them.
    """
    manifest_row = manifest_row or {}
    extra = manifest_row.get("extra") or {}
    published = manifest_row.get("published_at")
    return {
        "manifest_id": doc["manifest_id"],
        "source_system": manifest_row.get("source_system"),
        "source_id": manifest_row.get("source_id"),
        "source_url": manifest_row.get("doc_url"),
        "title": manifest_row.get("title"),
        "doc_type": extra.get("doc_type") or (cfg.doc_type if cfg else None),
        "language": extra.get("default_language")
        or (cfg.default_language if cfg else None),
        "published_at": published.isoformat() if published else None,
    }


def _auth_headers(cfg: SourceConfig | None, settings) -> dict[str, str]:
    """Per-request auth for this source. Applied per request, not on the client,
    because one batch mixes sources: Confluence credentials must not ride along
    on a public ECB fetch."""
    if cfg is None or cfg.type != "confluence":
        return {}
    if not settings.confluence_email or not settings.confluence_api_token:
        raise ParseFailure(
            f"confluence source {cfg.id!r} needs RAG_CONFLUENCE_EMAIL and "
            f"RAG_CONFLUENCE_API_TOKEN"
        )
    token = base64.b64encode(
        f"{settings.confluence_email}:{settings.confluence_api_token}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch(
    client: httpx.Client, manifest_row: dict, cfg: SourceConfig | None, settings
) -> tuple[bytes, str | None]:
    """Raw bytes for one document, plus the served content type if there is one.

    - file:/// — a local_files doc_url, which holds the file's path relative
      to the source's configured directory (see the discover reader): read
      the file under cfg.path. There is no served content type, so the
      caller works it out from the %PDF bytes and the .pdf suffix.
    - a Confluence page — the doc_url is the human permalink, which serves the
      whole Confluence UI, so fetch the body through the API instead.
    - a Confluence attachment — same reason: the doc_url is the /wiki/download
      link, which only accepts a browser session and answers Basic auth with
      401, so fetch the bytes through the API instead.
    - everything else — HTTP(S) via httpx.
    """
    url = manifest_row["doc_url"]
    extra = manifest_row.get("extra") or {}
    headers = _auth_headers(cfg, settings)

    if url.startswith(_FILE_URL_PREFIX):
        if cfg is None or not cfg.path:
            raise ParseFailure(
                f"{url} needs a local_files source with a configured path"
            )
        # exact inverse of the reader's f"file:///{rel}" — it does not
        # percent-encode, so this must not unquote
        rel = url[len(_FILE_URL_PREFIX):]
        return (Path(cfg.path) / rel).read_bytes(), None
    elif extra.get("confluence_kind") == "page":
        page_id = extra.get("page_id")
        if not page_id:
            # ParseFailure, not KeyError: a malformed sighting must fail this one
            # document, not take the whole batch down
            raise ParseFailure(f"confluence sighting for {url} has no page_id")
        return _fetch_confluence_page(client, page_id, cfg, headers)
    elif extra.get("confluence_kind") == "attachment":
        page_id = extra.get("page_id")
        attachment_id = extra.get("attachment_id")
        if not page_id or not attachment_id:
            raise ParseFailure(
                f"confluence attachment sighting for {url} has no "
                f"page_id/attachment_id"
            )
        return _fetch_confluence_attachment(
            client, page_id, attachment_id, cfg, headers
        )
    else:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.content, response.headers.get("content-type")


def _fetch_confluence_page(
    client: httpx.Client, page_id: str, cfg: SourceConfig | None, headers: dict
) -> tuple[bytes, str]:
    """A Confluence page body as rendered HTML.

    export_view, not the raw storage format: storage carries Confluence's own
    macro markup (<ac:structured-macro>, <ri:attachment>), which is not HTML and
    which the parsers cannot read. export_view has the macros expanded.

    The served content type is application/json, so this hands back the
    Confluence content type explicitly rather than let sniffing decide.
    """
    if cfg is None or not cfg.base_url:
        raise ParseFailure(
            f"confluence page {page_id} needs a source with a configured base_url"
        )
    response = client.get(
        f"{cfg.base_url.rstrip('/')}/wiki/api/v2/pages/{page_id}",
        params={"body-format": "export_view"},
        headers={**headers, "Accept": "application/json"},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseFailure(
            f"confluence page {page_id} did not return JSON: {exc}"
        ) from exc
    body = ((payload.get("body") or {}).get("export_view") or {}).get("value")
    if not body:
        raise EmptyDocument(f"confluence page {page_id} returned an empty body")
    return body.encode("utf-8"), CONFLUENCE_CONTENT_TYPE


def _fetch_confluence_attachment(
    client: httpx.Client,
    page_id: str,
    attachment_id: str,
    cfg: SourceConfig | None,
    headers: dict,
) -> tuple[bytes, str | None]:
    """An attachment's bytes, through the v1 content API.

    This is the path the v2 attachment's own `downloadLink` names. The
    /wiki/download/attachments/... URL in doc_url is the browser route: it
    answers Basic auth with a 401 HTML error page, which then reaches the
    parsers as text/html (measured 2026-07-30).
    """
    if cfg is None or not cfg.base_url:
        raise ParseFailure(
            f"confluence attachment {attachment_id} needs a source with a "
            f"configured base_url"
        )
    response = client.get(
        f"{cfg.base_url.rstrip('/')}/wiki/rest/api/content/{page_id}"
        f"/child/attachment/{attachment_id}/download",
        headers=headers,
    )
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
    config_path: str | Path | None,
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
    # never None: run_parse_batch raises on a batch doc without a sighting
    manifest_row: dict,
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
        raw, header_ct = _fetch(client, manifest_row, cfg, settings)
        content_sha = hashlib.sha256(raw).hexdigest()
        content_type = detect_content_type(header_ct, url, raw)
        markdown, parser_version = parse_document(
            raw, content_type, cfg.pdf_parser if cfg else None
        )
        text_sha = hashlib.sha256(markdown.encode()).hexdigest()
        quality = {"markdown_chars": len(markdown)}
        if content_type == "application/pdf":
            quality.update(pdf_quality(raw))
    # before the ParseFailure clause below: EmptyDocument is a subclass of it.
    # A document with no text is not a failure — it is recorded, carries no
    # chunks, and the embed stage drops its vectors and marks the ledger 'empty'.
    except EmptyDocument as exc:
        logger.info("empty document %s: %s", url, exc)
        outcome = {
            "status": "skipped_empty",
            "error": None,
            # content_sha and parser_version are left out: the raise can come
            # from the fetch, before either exists, and both columns are nullable
            "text_sha": EMPTY_TEXT_SHA,
            "node_count": 0,
            "parse_quality": {"markdown_chars": 0, "empty_reason": str(exc)},
        }
        with engine.begin() as conn:
            # a doc that had text before and is empty now must not keep its chunks
            conn.execute(
                delete(staged_nodes).where(
                    staged_nodes.c.manifest_id == doc["manifest_id"]
                )
            )
            _write_outcome_conn(conn, doc, outcome)
        return outcome
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
