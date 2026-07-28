# Stage-4 (PARSE+CHUNK) image — the future Nomad batch-job artifact.
# OCR note (block 2): when scanned sources arrive, add tesseract-ocr +
# tesseract-ocr-deu here and set use_ocr=True in parsers.py.
FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY config ./config
ENV RAG_SOURCES_PATH=/app/config/sources.yaml
