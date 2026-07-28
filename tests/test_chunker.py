from rag_parse_chunk.chunker import chunk_markdown

BASE_META = {"source_system": "web_rss", "source_url": "https://x/doc"}


def test_section_path_from_headings():
    md = "# Title\n\nIntro.\n\n## Section A\n\nBody text here."
    chunks = chunk_markdown(md, BASE_META, 700, 100)
    paths = {c["metadata"]["section_path"] for c in chunks}
    assert any("Title" in p for p in paths)


def test_base_metadata_on_every_chunk():
    md = "# T\n\n" + "word " * 2000
    chunks = chunk_markdown(md, BASE_META, 700, 100)
    assert len(chunks) > 1  # long doc actually splits
    for c in chunks:
        assert c["metadata"]["source_url"] == "https://x/doc"


def test_node_seq_is_dense_and_ordered():
    md = "# T\n\n" + "word " * 2000
    chunks = chunk_markdown(md, BASE_META, 700, 100)
    assert [c["node_seq"] for c in chunks] == list(range(len(chunks)))


def test_chunk_size_respected():
    md = "# T\n\n" + "word " * 3000
    chunks = chunk_markdown(md, BASE_META, 200, 20)
    assert all(len(c["text"].split()) <= 220 for c in chunks)


def test_deterministic():
    md = "# T\n\nSome body.\n\n## S\n\n" + "word " * 500
    assert chunk_markdown(md, BASE_META, 300, 30) == chunk_markdown(
        md, BASE_META, 300, 30
    )
