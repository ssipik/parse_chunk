"""Format parsers: raw bytes → Markdown (ARCHITECTURE.md → Parsing).

Every source format is converted to Markdown, keeping headings, tables and
lists, so chunking (chunking) never sees source formats. One parser per
content type, registered in PARSER_REGISTRY; parser_version strings embed
the library version so an upgrade changes the version and the change-detection logic
reprocesses already-ingested documents on the next run.
"""

from __future__ import annotations

import importlib.metadata
import re

import pymupdf
import pymupdf4llm
import trafilatura
from markdownify import markdownify

from rag_config import load_params

__all__ = [
    "ParseFailure",
    "EmptyDocument",
    "CONFLUENCE_CONTENT_TYPE",
    "PARSER_REGISTRY",
    "PDF_PARSERS",
    "detect_content_type",
    "parse_document",
    "pdf_quality",
]


class ParseFailure(Exception):
    """Parsing produced no usable text. Recorded against the one document;
    the batch carries on."""


class EmptyDocument(ParseFailure):
    """The document itself has no text — an empty Confluence page, a scanned
    PDF with OCR off. Not a failure: the doc is recorded as 'skipped_empty'
    and carries no vectors (see parse_chunk.py). A ParseFailure subclass so
    callers that only care about "no text came out" keep working."""


def _html_parser_version() -> str:
    return f"trafilatura-{trafilatura.__version__}-md1"


def _confluence_parser_version() -> str:
    return f"markdownify-{importlib.metadata.version('markdownify')}-confluence1"


def _pdf_parser_version(layout: bool) -> str:
    # pymupdf-layout is in here because it, not pymupdf4llm, does the layout
    # analysis: its version changes the Markdown, so it must trigger reparses.
    # The two modes carry different versions, so switching a source between
    # them reprocesses that source's documents.
    engine = (
        f"+layout{importlib.metadata.version('pymupdf-layout')}"
        if layout
        else "-fast"
    )
    return f"pymupdf4llm-{importlib.metadata.version('pymupdf4llm')}{engine}-md1"


# Inline HTML pymupdf4llm emits for highlighted/underlined runs, and the
# markers it wraps around text drawn on top of images. The text between the
# markers is real (chart legends, axis labels) — only the markers go.
_INLINE_NOISE = re.compile(r"</?(?:mark|u)>|<!-- (?:Start|End) of picture text -->")

# an ATX heading, split into its '### ' marker and its text
_HEADING_LINE = re.compile(r"(?m)^(#{1,6} )(.*)$")


def _strip_parser_noise(markdown: str) -> str:
    """Drop pymupdf4llm's inline HTML and picture-text markers, collapse the
    blank lines that leaves behind, and clean up heading lines. `<br>` inside
    table cells and emphasis in body text both stay.

    Both cleanups are about heading text, because the chunker turns it into
    section_path, which rides into every chunk's metadata and its citation:
    - trailing spaces (how pymupdf4llm joins spans) make '/Titel /'
    - a PDF heading set entirely in bold comes out as '# **Titel**'
    """
    without_markers = _INLINE_NOISE.sub("", markdown)
    unpadded = re.sub(r"[ \t]+$", "", without_markers, flags=re.MULTILINE)
    unbolded = _HEADING_LINE.sub(
        lambda m: m.group(1) + re.sub(r"\*\*|__", "", m.group(2)), unpadded
    )
    return re.sub(r"\n{3,}", "\n\n", unbolded).strip()


def parse_html(raw: bytes) -> tuple[str, str]:
    """HTML → Markdown via trafilatura (source connectors): it drops navigation,
    footers and the like, and keeps headings, tables and lists."""
    markdown = trafilatura.extract(
        raw.decode("utf-8", errors="replace"),
        output_format="markdown",
        include_formatting=True,
        include_tables=True,
        with_metadata=False,
    )
    if not markdown or not markdown.strip():
        raise EmptyDocument("trafilatura extracted no content")
    return markdown, _html_parser_version()


def parse_confluence(raw: bytes) -> tuple[str, str]:
    """A Confluence page's rendered body (export_view) → Markdown.

    markdownify, not trafilatura: trafilatura looks for an article inside a full
    web page and drops the rest. A Confluence body is already just the article,
    so that only costs content — measured 2026-07-29 on a rendered page, it
    dropped every heading (which the chunker turns into section_path, so it
    reaches the citations) and threw away short pages entirely. markdownify does
    a straight tag-by-tag conversion with no content detection.

    Links are stripped to their text, as parse_html does: a chunk's citation is
    the source_url in its metadata, not a URL inside the text.
    """
    html = raw.decode("utf-8", errors="replace")
    markdown = markdownify(html, heading_style="ATX", strip=["a", "img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if not any(char.isalnum() for char in markdown):
        raise EmptyDocument("the Confluence page body has no text")
    return markdown, _confluence_parser_version()


def _parse_pdf(raw: bytes, layout: bool) -> tuple[str, str]:
    """PDF → Markdown via pymupdf4llm (runs locally, no network).

    pymupdf4llm.use_layout() is a process-global switch, so it is set on every
    call rather than once at import — a batch may mix sources that ask for
    different modes. Both modes are deterministic and unaffected by what was
    parsed before them.

    OCR stays off until there are scanned sources. Turning it on means
    use_ocr=True plus Tesseract and the `deu` traineddata in the batch image,
    and the plan stage needs its OCR cost class at the same time. The fast
    mode has no OCR at all — it warns and ignores the argument.
    """
    pymupdf4llm.use_layout(layout)
    options = {"use_ocr": False} if layout else {}
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        markdown = pymupdf4llm.to_markdown(doc, show_progress=False, **options)
    markdown = _strip_parser_noise(markdown)
    # a text-free page can still come back as bare Markdown punctuation (a
    # stray '#'), so emptiness is decided on alphanumerics, not on length
    if not any(char.isalnum() for char in markdown):
        raise EmptyDocument("pymupdf4llm extracted no text (scanned PDF? OCR is off)")
    return markdown, _pdf_parser_version(layout)


def parse_pdf(raw: bytes) -> tuple[str, str]:
    """PDF → Markdown with the pymupdf-layout engine: resolves columns and
    detects tables, which is what the fund factsheets and PRIIPs documents
    need — LiteParse merged their columns line by line (see eval/README.md).
    ~3–5 s per document. The default for every source."""
    return _parse_pdf(raw, layout=True)


def parse_pdf_fast(raw: bytes) -> tuple[str, str]:
    """PDF → Markdown without the layout engine: ~5× faster, but on the local
    corpus it finds no tables and about a third of the headings. Only for
    sources whose PDFs are single-column prose, set per source via
    `pdf_parser: fast` in sources.yaml."""
    return _parse_pdf(raw, layout=False)


# Not a real media type: the fetcher tags a Confluence page body with it so the
# rendered wiki HTML reaches parse_confluence instead of parse_html.
CONFLUENCE_CONTENT_TYPE = "application/vnd.confluence.export+html"

PARSER_REGISTRY = {
    "text/html": parse_html,
    "application/xhtml+xml": parse_html,
    "application/pdf": parse_pdf,
    CONFLUENCE_CONTENT_TYPE: parse_confluence,
}

# PDF parsers a source may pick between (sources.yaml `pdf_parser`); the
# registry default above is 'layout'. HTML has no such choice, and a per-source
# override cannot span content types anyway: one source (ECB press) serves both
# HTML pages and PDFs.
PDF_PARSERS = {
    "layout": parse_pdf,
    "fast": parse_pdf_fast,
}

# A page under this many characters of extracted text carries its content in
# graphics the parsers cannot read — a chart or a scan, contributing nothing.
# The threshold comes from rag-config's parameters.yaml.


def pdf_quality(raw: bytes) -> dict:
    """Per-document signals for how much of a PDF the parser could actually
    read, so a document that parses 'successfully' but yields almost nothing
    is visible instead of silent.

    Measured off the PDF itself, not off the Markdown, so it stays comparable
    across parsers. Vector graphics (the fund factsheets' bar charts) do not
    count towards image_coverage — reading those needs get_drawings(), which
    is expensive; they show up as low text instead.
    """
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        pages = []
        for page in doc:
            blocks = page.get_text("blocks")
            page_area = abs(page.rect) or 1.0
            pages.append(
                (
                    sum(len(b[4].strip()) for b in blocks if b[6] == 0),
                    min(
                        sum(abs(pymupdf.Rect(b[:4])) for b in blocks if b[6] == 1)
                        / page_area,
                        1.0,
                    ),
                )
            )

    text_chars = sum(chars for chars, _ in pages)
    per_page = text_chars / (len(pages) or 1)
    low_text_chars_per_page = load_params().parse_chunk.low_text_chars_per_page
    return {
        "pages": len(pages),
        "text_chars": text_chars,
        "chars_per_page": round(per_page),
        "image_coverage": round(max((cov for _, cov in pages), default=0.0), 2),
        "pages_low_text": sum(
            1 for chars, _ in pages if chars < low_text_chars_per_page
        ),
        "low_text": per_page < low_text_chars_per_page,
    }


def detect_content_type(header_value: str | None, doc_url: str, raw: bytes) -> str:
    """Decide which content type to parse as: served header first, then
    cheap sniffing of the URL suffix and the leading bytes.

    Regulator sites sometimes serve PDFs as octet-stream, which the %PDF
    first bytes and the .pdf suffix catch. Anything else is returned as it
    came, and parse_document then rejects it as unsupported.
    """
    content_type = (header_value or "").split(";")[0].strip().lower()
    if content_type in PARSER_REGISTRY:
        return content_type
    if raw[:5] == b"%PDF-" or doc_url.lower().endswith(".pdf"):
        return "application/pdf"
    if raw.lstrip()[:1] == b"<":
        return "text/html"
    return content_type or "application/octet-stream"


def parse_document(
    raw: bytes, content_type: str, pdf_parser: str | None = None
) -> tuple[str, str]:
    """Call the parser registered for this content type; returns
    (markdown, parser_version). pdf_parser is the source's optional choice
    between PDF_PARSERS and is ignored for every other content type."""
    parser = PARSER_REGISTRY.get(content_type)
    if parser is None:
        raise ParseFailure(f"unsupported content type: {content_type!r}")
    if content_type == "application/pdf" and pdf_parser:
        if pdf_parser not in PDF_PARSERS:
            raise ParseFailure(
                f"unknown pdf_parser {pdf_parser!r} — pick one of "
                f"{', '.join(PDF_PARSERS)}"
            )
        parser = PDF_PARSERS[pdf_parser]
    return parser(raw)
