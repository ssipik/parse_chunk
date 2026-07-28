from datetime import datetime, timezone

import pytest

from rag_parse_chunk.parse_chunk import _fetch, base_metadata, unchanged_post_parse
from rag_parse_chunk.parsers import ParseFailure
from rag_parse_chunk.sources import SourceConfig

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

    def test_hashes_never_reach_the_payload(self):
        # they live on ingest_batch_docs; no query reads them from Qdrant
        meta = base_metadata(DOC, MANIFEST, CFG)
        assert not {"content_sha", "text_sha", "parser_version"} & set(meta)


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
            "file:///MR_DE_de_IE000716YHJ7_YES_2026-06-30.pdf",
            self._cfg(tmp_path),
        )
        assert raw == b"%PDF-1.7"
        assert header_ct is None  # no served type; detection uses magic/suffix

    def test_reads_nested_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.pdf").write_bytes(b"%PDF-x")
        raw, _ = _fetch(None, "file:///sub/a.pdf", self._cfg(tmp_path))
        assert raw == b"%PDF-x"

    def test_missing_file_raises_oserror(self, tmp_path):
        # caught per doc in _process_doc → status='error', batch continues
        with pytest.raises(OSError):
            _fetch(None, "file:///gone.pdf", self._cfg(tmp_path))

    def test_source_without_path_raises_parse_failure(self):
        with pytest.raises(ParseFailure):
            _fetch(None, "file:///a.pdf", None)
