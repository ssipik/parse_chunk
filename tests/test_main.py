import pytest

from rag_parse_chunk.__main__ import build_parser, main


def test_batch_id_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_batch_id_repeatable():
    args = build_parser().parse_args(["--batch-id", "1", "--batch-id", "2"])
    assert args.batch_ids == [1, 2]


def test_batch_id_must_be_an_int():
    # batch ids are bigint identities now, not "<run>-b<uuid>" strings
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--batch-id", "r1-babc"])


def test_main_missing_config_exits_2(capsys):
    rc = main(["--batch-id", "1", "--config", "/nonexistent/sources.yaml"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
