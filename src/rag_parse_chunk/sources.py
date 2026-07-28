from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str  # reader type, key into READER_REGISTRY
    source_system: str
    url: str | None = None
    # local_files sources: base directory to scan and the glob within it
    # (recursive by default; the reader keeps only regular files).
    path: str | None = None
    glob: str = "**/*"
    doc_type: str | None = None
    default_language: str | None = None
    # Which PDF parser this source's PDFs get (PDF_PARSERS in the parse_chunk
    # stage's parsers.py): unset = the layout engine, 'fast' = the ~5x faster
    # mode that finds no tables. Only the parse_chunk stage reads it.
    pdf_parser: str | None = None
    # False for sources that only show their newest items (an RSS feed lists
    # about 15): a document missing from a run has probably just dropped off
    # the end of the feed, so the delta stage must not treat it as deleted.
    detect_deletions: bool = True


def load_sources(path: str | Path) -> dict[str, SourceConfig]:
    raw = yaml.safe_load(Path(path).read_text())
    sources = (raw or {}).get("sources")
    if not sources:
        raise ValueError(f"no 'sources' section in {path}")
    return {sid: SourceConfig(id=sid, **cfg) for sid, cfg in sources.items()}
