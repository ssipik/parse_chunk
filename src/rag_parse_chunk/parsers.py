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

__all__ = [
    "ParseFailure",
    "PARSER_REGISTRY",
    "PDF_PARSERS",
    "detect_content_type",
    "parse_document",
    "pdf_quality",
]


class ParseFailure(Exception):
    """Parsing produced no usable text. Recorded against the one document;
    the batch carries on."""


def _html_parser_version() -> str:
    return f"trafilatura-{trafilatura.__version__}-md1"


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
        raise ParseFailure("trafilatura extracted no content")
    return markdown, _html_parser_version()


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
        raise ParseFailure("pymupdf4llm extracted no text (scanned PDF? OCR is off)")
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


PARSER_REGISTRY = {
    "text/html": parse_html,
    "application/xhtml+xml": parse_html,
    "application/pdf": parse_pdf,
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
_LOW_TEXT_CHARS_PER_PAGE = 200


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
    return {
        "pages": len(pages),
        "text_chars": text_chars,
        "chars_per_page": round(per_page),
        "image_coverage": round(max((cov for _, cov in pages), default=0.0), 2),
        "pages_low_text": sum(
            1 for chars, _ in pages if chars < _LOW_TEXT_CHARS_PER_PAGE
        ),
        "low_text": per_page < _LOW_TEXT_CHARS_PER_PAGE,
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
