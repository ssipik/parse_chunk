from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PG_DSN = "postgresql+psycopg://rag:rag@localhost:5433/rag_ingestion"
DEFAULT_USER_AGENT = "rag-pipeline-ingest/0.1"

# Chunk size and overlap (chunking asks for ~512–1024 tokens, ~15 % overlap).
# These are counted in words, not tokens, because chunker.py passes in a
# whitespace tokenizer — 700 German or English words is roughly 900–1000
# tokens.
DEFAULT_CHUNK_SIZE = 700
DEFAULT_CHUNK_OVERLAP = 100


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    http_timeout: float
    user_agent: str
    chunk_size: int
    chunk_overlap: int
    # Confluence Cloud basic auth (email + API token). Kept out of sources.yaml,
    # which is committed and baked into the images; Vault supplies these on the
    # cluster (ARCHITECTURE.md → Deployment). None until a confluence source runs.
    confluence_email: str | None
    confluence_api_token: str | None


def get_settings() -> Settings:
    return Settings(
        pg_dsn=os.environ.get("RAG_PG_DSN", DEFAULT_PG_DSN),
        http_timeout=float(os.environ.get("RAG_HTTP_TIMEOUT", "30")),
        user_agent=os.environ.get("RAG_HTTP_USER_AGENT", DEFAULT_USER_AGENT),
        confluence_email=os.environ.get("RAG_CONFLUENCE_EMAIL"),
        confluence_api_token=os.environ.get("RAG_CONFLUENCE_API_TOKEN"),
        chunk_size=int(os.environ.get("RAG_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)),
        chunk_overlap=int(os.environ.get("RAG_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP)),
    )
