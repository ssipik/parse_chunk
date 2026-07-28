"""The parse_chunk stage of the RAG ingestion pipeline (ARCHITECTURE.md → Ingestion orchestration).

Standalone stage module: settings, sources and db are copies of the same
files in the other stage packages, kept separate on purpose so each stage
stands alone. Merging them into one shared package waits until the
duplication actually causes trouble.
"""
