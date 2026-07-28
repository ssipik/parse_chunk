"""Compare PDF parsers on the local corpus: PDF -> Markdown -> PDF for eyeballing.

Every file in the dataset goes through every scenario in SCENARIOS. Per file
and scenario it writes the Markdown the parser produced plus that Markdown
re-rendered as a PDF, and prints a metrics table (also written as summary.csv).

Eval-only harness: its dependencies live in eval/pyproject.toml, not in the
stage package. The pymupdf scenario mirrors rag_parse_chunk.parsers.parse_pdf -
change both together.

    uv sync                                   # in this directory
    .venv/bin/python parser_eval.py           # writes ./out
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import time
from collections.abc import Callable
from pathlib import Path

import markdown as markdown_lib
import pymupdf
import pymupdf4llm
from liteparse import LiteParse

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"


_INLINE_NOISE = re.compile(r"</?(?:mark|u)>|<!-- (?:Start|End) of picture text -->")
_HEADING_LINE = re.compile(r"(?m)^(#{1,6} )(.*)$")


def _denoise(markdown: str) -> str:
    """Copy of rag_parse_chunk.parsers._strip_parser_noise — keep in step."""
    text = _INLINE_NOISE.sub("", markdown)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = _HEADING_LINE.sub(
        lambda m: m.group(1) + re.sub(r"\*\*|__", "", m.group(2)), text
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_liteparse(raw: bytes) -> str:
    """The parser the parse_chunk stage used until 2026-07-22: LiteParse, pages joined."""
    result = LiteParse(output_format="markdown", ocr_enabled=False, quiet=True).parse(raw)
    pages = [result.get_page(i).markdown for i in range(1, result.num_pages + 1)]
    return "\n\n".join(p for p in pages if p and p.strip())


def parse_pymupdf(raw: bytes) -> str:
    """The parser the parse_chunk stage uses now: pymupdf4llm on the pymupdf-layout engine.

    Mirrors rag_parse_chunk.parsers.parse_pdf, noise strip included, so what
    lands here is what the chunker sees.
    """
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        markdown = pymupdf4llm.to_markdown(doc, use_ocr=False, show_progress=False)
    return _denoise(markdown)


def parse_pymupdf_fast(raw: bytes) -> str:
    """pymupdf4llm without the layout engine — what a source gets from
    `pdf_parser: fast` in sources.yaml."""
    pymupdf4llm.use_layout(False)
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            markdown = pymupdf4llm.to_markdown(doc, show_progress=False)
    finally:
        pymupdf4llm.use_layout(True)
    return _denoise(markdown)


SCENARIOS: dict[str, Callable[[bytes], str]] = {
    "liteparse": parse_liteparse,
    "pymupdf": parse_pymupdf,
    "pymupdf_fast": parse_pymupdf_fast,
}

RENDER_CSS = """
body { font-family: sans-serif; font-size: 9px; }
h1 { font-size: 17px; } h2 { font-size: 15px; } h3 { font-size: 13px; }
h4, h5, h6 { font-size: 11px; }
table { border: 1px solid #888; border-collapse: collapse; }
th, td { border: 1px solid #888; padding: 2px 4px; }
th { background-color: #eee; }
code, pre { font-family: monospace; }
hr { color: #bbb; }
.banner { background-color: #eee; padding: 4px; font-family: monospace; font-size: 10px; }
"""

_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def markdown_to_pdf(text: str, out_path: Path, banner: str) -> None:
    """Render Markdown to PDF so the parser output can be read by eye.

    Two adjustments before rendering, neither touching the saved .md:
    - image links are dropped (they point at files no parser writes here)
    - HTML comments are escaped so parser noise like pymupdf4llm's picture-text
      markers stays visible instead of disappearing into the markup
    """
    body = _IMAGE_LINK.sub("", text).replace("<!--", "&lt;!--")
    rendered = markdown_lib.markdown(
        body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"]
    )
    doc_html = f'<div class="banner">{html.escape(banner)}</div>{rendered}'

    story = pymupdf.Story(html=doc_html, user_css=RENDER_CSS)
    writer = pymupdf.DocumentWriter(out_path)
    page_rect = pymupdf.paper_rect("a4")
    content_rect = page_rect + (30, 30, -30, -30)
    more = 1
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(content_rect)
        story.draw(device)
        writer.end_page()
    writer.close()


def metrics(text: str) -> dict[str, int]:
    """Cheap shape signals: how much structure the parser recovered."""
    return {
        "chars": len(text),
        "headings": len(re.findall(r"(?m)^#{1,6} ", text)),
        "tables": len(re.findall(r"(?m)^\|\s*-{3,}", text)),
        "rules": len(re.findall(r"(?m)^-{3,}\s*$", text)),
    }


def run(data_dir: Path, out_dir: Path, scenario_names: list[str]) -> list[dict]:
    """Parse and render every PDF under every scenario; returns the metric rows."""
    pdfs = sorted(data_dir.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no PDFs found in {data_dir}")

    rows: list[dict] = []
    for name in scenario_names:
        parser = SCENARIOS[name]
        scenario_dir = out_dir / name
        scenario_dir.mkdir(parents=True, exist_ok=True)

        for pdf_path in pdfs:
            raw = pdf_path.read_bytes()
            row = {"scenario": name, "file": pdf_path.name, "parse_ms": 0, "error": ""}
            try:
                started = time.perf_counter()
                text = parser(raw)
                row["parse_ms"] = round((time.perf_counter() - started) * 1000)
            except Exception as exc:  # one bad document must not stop the run
                row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                print(f"  {name:10s} {pdf_path.name}  FAILED: {row['error']}")
                continue

            row.update(metrics(text))
            (scenario_dir / f"{pdf_path.stem}.md").write_text(text, encoding="utf-8")
            banner = f"[{name}] {pdf_path.name} - {row['parse_ms']} ms, {row['chars']} chars"
            try:
                markdown_to_pdf(text, scenario_dir / f"{pdf_path.stem}.pdf", banner)
            except Exception as exc:
                row["error"] = f"render failed: {type(exc).__name__}: {exc}"
            rows.append(row)
            print(f"  {name:10s} {pdf_path.name}  {row['parse_ms']:>6} ms  {row['error']}")

    return rows


def print_summary(rows: list[dict]) -> None:
    header = f"{'file':44s} {'scenario':10s} {'ms':>6} {'chars':>7} {'head':>5} {'tbl':>4} {'hr':>4}"
    print(f"\n{header}\n{'-' * len(header)}")
    for row in sorted(rows, key=lambda r: (r["file"], r["scenario"])):
        print(
            f"{row['file'][:44]:44s} {row['scenario']:10s} {row['parse_ms']:>6} "
            f"{row.get('chars', 0):>7} {row.get('headings', 0):>5} "
            f"{row.get('tables', 0):>4} {row.get('rules', 0):>4} {row['error']}"
        )


def write_csv(rows: list[dict], path: Path) -> None:
    fields = ["scenario", "file", "parse_ms", "chars", "headings", "tables", "rules", "error"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({f: row.get(f, "") for f in fields} for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--scenarios",
        default=",".join(SCENARIOS),
        help=f"comma-separated subset of: {', '.join(SCENARIOS)}",
    )
    args = parser.parse_args()

    names = [n.strip() for n in args.scenarios.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}")

    rows = run(args.data_dir, args.out_dir, names)
    print_summary(rows)
    write_csv(rows, args.out_dir / "summary.csv")
    print(f"\nwrote {args.out_dir}")


if __name__ == "__main__":
    main()
