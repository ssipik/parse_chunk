# Stage-4 (PARSE+CHUNK) image — the future Nomad batch-job artifact.
# OCR note (block 2): when scanned sources arrive, add tesseract-ocr +
# tesseract-ocr-deu here and set use_ocr=True in parsers.py.
FROM python:3.13-slim

WORKDIR /app

# git is here for one reason: rag-config is a git dependency, and both uv and
# pip shell out to git to fetch one.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# Dependencies come pinned from uv.lock (own layer: rebuilds skip it unless the
# lock changes); the project itself is installed on top. `uv sync` rather than
# `pip install` of an exported requirements file: pip turns on hash checking as
# soon as any requirement carries a hash, and a git dependency cannot be hashed,
# so the export path fails on rag-config. uv verifies against the lock instead.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The venv sits outside /app because compose mounts ./src over /app, which would
# otherwise bury it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock ./
# rag-config lives in a private repo, so the dependency layer needs read access.
# The token arrives as a BuildKit secret — never an ARG or ENV, which would stay
# in the image history — and the .netrc it writes is removed inside the same RUN,
# so no layer ever holds it. Optional: with no secret passed the block is a
# no-op, which is what a public repo or a cached layer needs.
#   docker build --secret id=git_token,env=BB_TOKEN .
RUN --mount=type=secret,id=git_token \
    if [ -s /run/secrets/git_token ]; then \
      printf 'machine bitbucket.org login x-token-auth password %s\n' \
        "$(cat /run/secrets/git_token)" > /root/.netrc && chmod 600 /root/.netrc; \
    fi; \
    uv sync --frozen --no-dev --no-install-project; \
    status=$?; rm -f /root/.netrc; exit $status

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

