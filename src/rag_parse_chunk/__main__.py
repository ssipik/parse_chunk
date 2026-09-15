"""Dev/ops entrypoint: ``python -m rag_parse_chunk``.

Thin argparse wrapper over :func:`rag_parse_chunk.parse_chunk.run_parse_batch`
— the library function remains the API that Airflow tasks import directly;
keep all stage logic out of this module.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

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
        "— on the cluster each Nomad dispatch gets exactly one",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for batch_id in args.batch_ids:
        try:
            report = run_parse_batch(config_path=None, batch_id=batch_id)
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"batch {batch_id}: {report.counts}")
    return 0


if __name__ == "__main__":
    # Only needed when running in development. In production the env variables are provided by Vault.
    load_dotenv()
    raise SystemExit(main())
