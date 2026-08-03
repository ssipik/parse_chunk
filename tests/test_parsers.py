from pathlib import Path

import pytest

from rag_parse_chunk.parsers import (
    CONFLUENCE_CONTENT_TYPE,
    EmptyDocument,
    ParseFailure,
    _strip_parser_noise,
    detect_content_type,
    parse_confluence,
    parse_document,
    parse_html,
    pdf_quality,
)

FIXTURES = Path(__file__).parent / "fixtures"

# a syntactically minimal but real single-page PDF (one text object)
TINY_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 60>>stream\n"
    b"BT /F1 18 Tf 72 720 Td (Monetary policy decisions) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

# the same PDF with no content stream — a scanned page looks like this to a
# parser with OCR off
BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class TestDetectContentType:
    def test_header_wins(self):
        assert detect_content_type("text/html; charset=utf-8", "u", b"x") == "text/html"

    def test_pdf_magic_beats_missing_header(self):
        assert detect_content_type(None, "u", b"%PDF-1.7 ...") == "application/pdf"

    def test_pdf_url_suffix(self):
        # octet-stream header, no magic readable yet → the suffix decides
        assert (
            detect_content_type("application/octet-stream", "https://x/doc.pdf", b"??")
            == "application/pdf"
        )

    def test_html_sniff(self):
        assert detect_content_type(None, "u", b"  <!DOCTYPE html><p>x") == "text/html"

    def test_unknown_passes_through(self):
        assert detect_content_type("image/png", "u", b"\x89PNG") == "image/png"


class TestParseHtml:
    def test_extracts_structured_markdown(self):
        raw = (FIXTURES / "ecb_press_sample.html").read_bytes()
        markdown, version = parse_html(raw)
        assert "# Monetary policy decisions" in markdown
        assert "## Key interest rates" in markdown
        assert "| Deposit facility | 2.00 |" in markdown  # tables preserved
        assert "Privacy" not in markdown  # boilerplate stripped
        assert version.startswith("trafilatura-")

    def test_empty_html_is_an_empty_document(self):
        with pytest.raises(EmptyDocument):
            parse_html(b"<html><body></body></html>")


class TestParseConfluence:
    """A Confluence body is already the article, so it goes through markdownify
    rather than trafilatura — see the parser's docstring."""

    @staticmethod
    def _markdown():
        raw = (FIXTURES / "confluence_export_view.html").read_bytes()
        return parse_document(raw, CONFLUENCE_CONTENT_TYPE)

    def test_keeps_headings(self):
        # the chunker turns headings into section_path, which reaches citations
        markdown, _ = self._markdown()
        assert "# Zweck" in markdown
        assert "## Risikoarten" in markdown
        assert "## Ablauf" in markdown

    def test_keeps_tables_and_lists(self):
        markdown, _ = self._markdown()
        assert "| Adressenausfallrisiko | ja | Kreditrisikocontrolling |" in markdown
        assert "1. Erhebung der Risiken je Fachbereich" in markdown

    def test_keeps_expanded_macro_text(self):
        # export_view renders macros; their text is content, not chrome
        markdown, _ = self._markdown()
        assert "Stichtag ist der 30.06." in markdown

    def test_links_become_plain_text(self):
        markdown, _ = self._markdown()
        assert "Risikobericht Q2.pdf" in markdown
        assert "/wiki/download/attachments" not in markdown

    def test_short_page_survives(self):
        # trafilatura discarded these entirely, which is why it is not used here
        markdown, _ = parse_confluence(
            b"<h1>Meldefristen</h1><p>Meldung bis zum 15. Werktag.</p>"
        )
        assert "# Meldefristen" in markdown
        assert "15. Werktag" in markdown

    def test_empty_body_is_an_empty_document(self):
        # not an error: the page is recorded as skipped_empty and carries no
        # vectors (see parse_chunk._process_doc)
        with pytest.raises(EmptyDocument):
            parse_confluence(b"<div><span></span></div>")

    def test_parser_version_is_distinct_from_the_html_parser(self):
        # so registering this parser does not reprocess the ECB HTML pages
        _, confluence_version = self._markdown()
        _, html_version = parse_html((FIXTURES / "ecb_press_sample.html").read_bytes())
        assert confluence_version != html_version
        assert confluence_version.startswith("markdownify-")


class TestParsePdf:
    def test_extracts_text(self):
        markdown, version = parse_document(TINY_PDF, "application/pdf")
        assert "Monetary policy decisions" in markdown
        assert version.startswith("pymupdf4llm-")
        assert "+layout" in version  # the layout engine drives the output

    def test_no_text_is_an_empty_document(self):
        with pytest.raises(EmptyDocument, match="no text"):
            parse_document(BLANK_PDF, "application/pdf")

    def test_empty_document_is_still_a_parse_failure(self):
        # _process_doc's generic except clause catches ParseFailure, so the
        # EmptyDocument clause has to come first — this pins the relationship
        assert issubclass(EmptyDocument, ParseFailure)


class TestPdfParserOverride:
    def test_fast_mode_is_a_different_parser_version(self):
        # the versions must differ, or switching a source between modes would
        # not reprocess that source's documents
        _, layout_version = parse_document(TINY_PDF, "application/pdf")
        markdown, fast_version = parse_document(TINY_PDF, "application/pdf", "fast")
        assert "Monetary policy decisions" in markdown
        assert fast_version.endswith("-fast-md1")
        assert fast_version != layout_version

    def test_layout_still_works_after_a_fast_parse(self):
        # use_layout() is process-global state, so the modes must not leak
        parse_document(TINY_PDF, "application/pdf", "fast")
        _, version = parse_document(TINY_PDF, "application/pdf")
        assert "+layout" in version

    def test_unknown_parser_name_fails(self):
        with pytest.raises(ParseFailure, match="unknown pdf_parser"):
            parse_document(TINY_PDF, "application/pdf", "docling")

    def test_ignored_for_other_content_types(self):
        raw = (FIXTURES / "ecb_press_sample.html").read_bytes()
        _, version = parse_document(raw, "text/html", "fast")
        assert version.startswith("trafilatura-")


class TestPdfQuality:
    def test_reads_page_and_text_signals(self):
        quality = pdf_quality(TINY_PDF)
        assert quality["pages"] == 1
        assert quality["text_chars"] == len("Monetary policy decisions")
        assert quality["image_coverage"] == 0.0

    def test_flags_a_page_with_no_readable_text(self):
        quality = pdf_quality(BLANK_PDF)
        assert quality["low_text"] is True
        assert quality["pages_low_text"] == 1
        assert quality["chars_per_page"] == 0


class TestStripParserNoise:
    def test_drops_inline_html_and_picture_markers(self):
        raw = (
            "# T\n\n<!-- Start of picture text -->\nAxis label\n"
            "<!-- End of picture text -->\n\n\n\n<mark>PASSIVE</mark> <u>link</u>\n"
        )
        assert _strip_parser_noise(raw) == "# T\n\nAxis label\n\nPASSIVE link"

    def test_keeps_line_breaks_inside_table_cells(self):
        raw = "|Bloomberg<br>Ticker|FTSS GY|"
        assert _strip_parser_noise(raw) == raw

    def test_drops_trailing_spaces(self):
        # '# Titel ' would otherwise make section_path '/Titel /'
        assert _strip_parser_noise("# Titel \n\n## Zweck \n\nText \n") == (
            "# Titel\n\n## Zweck\n\nText"
        )

    def test_unbolds_headings_but_not_body_text(self):
        raw = "## **Zweck**\n\nDas ist **wichtig**.\n"
        assert _strip_parser_noise(raw) == "## Zweck\n\nDas ist **wichtig**."


def test_unsupported_type_fails():
    with pytest.raises(ParseFailure, match="unsupported"):
        parse_document(b"GIF89a", "image/gif")
