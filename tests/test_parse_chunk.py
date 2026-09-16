import base64
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from rag_parse_chunk.parse_chunk import (
    _auth_headers,
    _fetch,
    base_metadata,
    unchanged_post_parse,
)
from rag_parse_chunk.parsers import CONFLUENCE_CONTENT_TYPE, ParseFailure
from rag_parse_chunk.settings import get_settings
from rag_config import SourceConfig

# get_settings() reads the environment, so build the Confluence credentials on
# top of it rather than depend on what is exported
SETTINGS = replace(
    get_settings(), confluence_email="u@e.x", confluence_api_token="tok"
)

CFG = SourceConfig(
    id="feed_a",
    type="rss",
    source_system="web_rss",
    url="https://e.x/feed",
    doc_type="press_release",
    default_language="en",
)

# a batch-doc row: identity only — the document's attributes live on the
# manifest sighting it points at
DOC = {"manifest_id": 7}
MANIFEST = {
    "manifest_id": 7,
    "source_system": "web_rss",
    "doc_url": "https://e.x/d1",
    "source_id": "feed_a",
}


class TestUnchangedPostParse:
    def test_no_ledger_row_means_changed(self):
        assert not unchanged_post_parse(None, "sha", "v1")

    def test_same_text_same_parser_skips(self):
        row = {"text_sha": "sha", "parser_version": "v1"}
        assert unchanged_post_parse(row, "sha", "v1")

    def test_parser_upgrade_reprocesses(self):
        # change detection: reprocess when text_sha OR parser_version changed
        row = {"text_sha": "sha", "parser_version": "v1"}
        assert not unchanged_post_parse(row, "sha", "v2")

    def test_changed_text_reprocesses(self):
        row = {"text_sha": "old", "parser_version": "v1"}
        assert not unchanged_post_parse(row, "new", "v1")


class TestBaseMetadata:
    def test_manifest_extra_wins_over_source_config(self):
        manifest = {
            **MANIFEST,
            "title": "T",
            "published_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "extra": {"doc_type": "statistical_press_release", "default_language": "de"},
        }
        meta = base_metadata(DOC, manifest, CFG)
        assert meta["doc_type"] == "statistical_press_release"
        assert meta["language"] == "de"
        assert meta["published_at"] == "2026-07-01T00:00:00+00:00"

    def test_source_config_fallback_without_manifest(self):
        meta = base_metadata(DOC, None, CFG)
        assert meta["doc_type"] == "press_release"
        assert meta["language"] == "en"
        assert meta["title"] is None

    def test_identity_fields_always_present(self):
        meta = base_metadata(DOC, MANIFEST, None)
        assert meta["manifest_id"] == 7
        assert meta["source_url"] == "https://e.x/d1"

    def test_provenance_reaches_the_payload(self):
        # the agent filters searches on source_system/source_id, so they have to
        # be in the Qdrant payload — Postgres is not in the query path
        meta = base_metadata(DOC, MANIFEST, CFG)
        assert meta["source_system"] == "web_rss"
        assert meta["source_id"] == "feed_a"

    def test_hashes_never_reach_the_payload(self):
        # they live on ingest_batch_docs; no query reads them from Qdrant
        meta = base_metadata(DOC, MANIFEST, CFG)
        assert not {"content_sha", "text_sha", "parser_version"} & set(meta)


def _row(doc_url, extra=None):
    """A manifest sighting as _fetch reads it: the URL and the reader's extra."""
    return {**MANIFEST, "doc_url": doc_url, "extra": extra or {}}


class TestFetchLocalFile:
    """file:/// doc_urls from the local_files reader resolve against the
    source's configured path — httpx never sees them."""

    @staticmethod
    def _cfg(tmp_path):
        return SourceConfig(
            id="local_pdfs",
            type="local_files",
            source_system="local_files",
            path=str(tmp_path),
        )

    def test_reads_bytes_relative_to_source_path(self, tmp_path):
        (tmp_path / "MR_DE_de_IE000716YHJ7_YES_2026-06-30.pdf").write_bytes(b"%PDF-1.7")
        raw, header_ct = _fetch(
            None,
            _row("file:///MR_DE_de_IE000716YHJ7_YES_2026-06-30.pdf"),
            self._cfg(tmp_path),
            SETTINGS,
        )
        assert raw == b"%PDF-1.7"
        assert header_ct is None  # no served type; detection uses magic/suffix

    def test_reads_nested_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.pdf").write_bytes(b"%PDF-x")
        raw, _ = _fetch(None, _row("file:///sub/a.pdf"), self._cfg(tmp_path), SETTINGS)
        assert raw == b"%PDF-x"

    def test_missing_file_raises_oserror(self, tmp_path):
        # caught per doc in _process_doc → status='error', batch continues
        with pytest.raises(OSError):
            _fetch(None, _row("file:///gone.pdf"), self._cfg(tmp_path), SETTINGS)

    def test_source_without_path_raises_parse_failure(self):
        with pytest.raises(ParseFailure):
            _fetch(None, _row("file:///a.pdf"), None, SETTINGS)


CONFLUENCE_CFG = SourceConfig(
    id="confluence_risk",
    type="confluence",
    source_system="confluence",
    base_url="https://acme.atlassian.net",
    space_key="RISK",
    label="RAG",
    include_attachments=True,
    doc_type="wiki_page",
    default_language="de",
)

PAGE_ROW = _row(
    "https://acme.atlassian.net/wiki/spaces/RISK/pages/123",
    {"confluence_kind": "page", "page_id": "123"},
)
ATTACHMENT_ROW = _row(
    "https://acme.atlassian.net/wiki/download/attachments/123/report.pdf",
    {"confluence_kind": "attachment", "attachment_id": "att1", "page_id": "123"},
)


class TestAuthHeaders:
    def test_no_auth_for_public_sources(self):
        assert _auth_headers(CFG, SETTINGS) == {}
        assert _auth_headers(None, SETTINGS) == {}

    def test_confluence_gets_basic_auth(self):
        headers = _auth_headers(CONFLUENCE_CFG, SETTINGS)
        scheme, token = headers["Authorization"].split(" ")
        assert scheme == "Basic"
        assert base64.b64decode(token).decode() == "u@e.x:tok"

    def test_missing_credentials_raise_parse_failure(self):
        bare = replace(SETTINGS, confluence_email=None, confluence_api_token=None)
        with pytest.raises(ParseFailure):
            _auth_headers(CONFLUENCE_CFG, bare)


class FakeClient:
    """Records the requests _fetch makes and replays canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        return self.responses.pop(0)


class FakeResponse:
    def __init__(self, payload=None, content=b"", content_type=None):
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type} if content_type else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class TestFetchConfluence:
    def test_page_body_comes_from_the_api_not_the_permalink(self):
        client = FakeClient(
            FakeResponse({"body": {"export_view": {"value": "<h1>Titel</h1>"}}})
        )
        raw, content_type = _fetch(client, PAGE_ROW, CONFLUENCE_CFG, SETTINGS)

        assert raw == b"<h1>Titel</h1>"
        # explicit, because the API serves application/json
        assert content_type == CONFLUENCE_CONTENT_TYPE
        call = client.calls[0]
        assert call["url"] == "https://acme.atlassian.net/wiki/api/v2/pages/123"
        assert call["params"] == {"body-format": "export_view"}
        assert call["headers"]["Authorization"].startswith("Basic ")

    def test_empty_body_raises_parse_failure(self):
        client = FakeClient(FakeResponse({"body": {"export_view": {"value": ""}}}))
        with pytest.raises(ParseFailure):
            _fetch(client, PAGE_ROW, CONFLUENCE_CFG, SETTINGS)

    def test_sighting_without_page_id_fails_only_that_doc(self):
        # ParseFailure is caught per doc; a KeyError would end the batch
        row = _row("https://acme.atlassian.net/wiki/spaces/RISK/pages/9", {
            "confluence_kind": "page"
        })
        with pytest.raises(ParseFailure):
            _fetch(FakeClient(), row, CONFLUENCE_CFG, SETTINGS)

    def test_source_without_base_url_raises_parse_failure(self):
        cfg = CONFLUENCE_CFG.model_copy(update={"base_url": None})
        with pytest.raises(ParseFailure):
            _fetch(FakeClient(), PAGE_ROW, cfg, SETTINGS)

    def test_attachment_bytes_come_from_the_api_not_the_download_url(self):
        client = FakeClient(
            FakeResponse(content=b"%PDF-1.7", content_type="application/pdf")
        )
        raw, content_type = _fetch(client, ATTACHMENT_ROW, CONFLUENCE_CFG, SETTINGS)

        assert raw == b"%PDF-1.7"
        # served type flows into the existing PDF path, no special casing
        assert content_type == "application/pdf"
        # the doc_url /wiki/download/... route answers Basic auth with a 401
        assert client.calls[0]["url"] == (
            "https://acme.atlassian.net/wiki/rest/api/content/123"
            "/child/attachment/att1/download"
        )
        assert client.calls[0]["headers"]["Authorization"].startswith("Basic ")

    def test_attachment_sighting_without_ids_fails_only_that_doc(self):
        row = _row(
            "https://acme.atlassian.net/wiki/download/attachments/9/report.pdf",
            {"confluence_kind": "attachment", "page_id": "9"},
        )
        with pytest.raises(ParseFailure):
            _fetch(FakeClient(), row, CONFLUENCE_CFG, SETTINGS)

    def test_credentials_do_not_ride_along_on_public_fetches(self):
        client = FakeClient(FakeResponse(content=b"<html/>", content_type="text/html"))
        _fetch(client, _row("https://www.ecb.europa.eu/x.html"), CFG, SETTINGS)
        assert client.calls[0]["headers"] == {}


class TestBatchWithoutDocuments:
    """No input is not an error: an unknown or empty batch parses nothing and succeeds."""

    class EmptyEngine:
        def connect(self):
            from contextlib import contextmanager

            @contextmanager
            def conn():
                class Result:
                    def mappings(self):
                        return []

                class Conn:
                    def execute(self, stmt):
                        return Result()

                yield Conn()

            return conn()

    def test_empty_batch_reports_nothing(self):
        import httpx

        from rag_parse_chunk.parse_chunk import run_parse_batch

        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        report = run_parse_batch(None, 999, engine=self.EmptyEngine(), client=client)
        assert report.batch_id == 999
        assert all(v == 0 for v in report.counts.values())
