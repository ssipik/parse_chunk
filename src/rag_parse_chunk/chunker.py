"""Markdown → chunk dicts (ARCHITECTURE.md → Chunking, default path).

MarkdownNodeParser splits on headings (the headings above a section are
kept as header_path metadata), then SentenceSplitter cuts anything still
over the size limit, at sentence boundaries. The whitespace tokenizer is
passed in on purpose: sizes are then counted in *words* (~0.7–0.8 of a
token budget) and stay the same on every run without network access; the
default would download tiktoken's vocab on first use, which batch
containers and tests must not do.
"""

from __future__ import annotations

from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import Document

__all__ = ["chunk_markdown"]


def chunk_markdown(
    markdown: str,
    base_metadata: dict,
    chunk_size: int,
    chunk_overlap: int,
) -> list[dict]:
    """Split the parsed Markdown into ordered chunk dicts.

    Each dict carries node_seq (the chunk's position in the document, which
    the embed stage hashes into the Qdrant point id), text, and the change detection
    metadata: base_metadata (source and doc-level fields, passed in by the
    caller) plus section_path, the headings above the chunk.
    """
    md_nodes = MarkdownNodeParser().get_nodes_from_documents(
        [Document(text=markdown)]
    )
    # include_metadata=True so header_path is kept when a section is cut in
    # two; it is the only field present at this point (ours is added below),
    # so it takes almost nothing off the chunk size limit.
    final_nodes = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        tokenizer=str.split,
        include_metadata=True,
    ).get_nodes_from_documents(md_nodes)

    chunks = []
    for seq, node in enumerate(final_nodes):
        text = node.get_content().strip()
        if not text:
            continue
        chunks.append(
            {
                "node_seq": seq,
                "text": text,
                "metadata": {
                    **base_metadata,
                    "section_path": node.metadata.get("header_path", "/"),
                },
            }
        )
    return chunks
