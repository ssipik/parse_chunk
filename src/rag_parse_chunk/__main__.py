"""Dev/ops entrypoint: ``python -m rag_parse_chunk``.

Thin argparse wrapper over :func:`rag_parse_chunk.parse_chunk.run_parse_batch`
— the library function remains the API that Airflow tasks import directly;
keep all stage logic out of this module.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from rag_parse_chunk.parse_chunk import run_parse_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_parse_chunk",
        description="PARSE+CHUNK stage: fetch, parse to Markdown, chunk, "
        "and stage one planned batch's documents.",
    )
    parser.add_argument(
        "--batch-id",
        required=True,
        type=int,
        action="append",
        dest="batch_ids",
        help="batch to process (a rag_plan batch id); repeatable for dev runs "
        "— in the DAG each mapped task gets exactly one",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("RAG_SOURCES_PATH", "config/sources.yaml"),
        help="path to sources.yaml (default: $RAG_SOURCES_PATH or %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for batch_id in args.batch_ids:
        try:
            report = run_parse_batch(args.config, batch_id)
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"batch {batch_id}: {report.counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
