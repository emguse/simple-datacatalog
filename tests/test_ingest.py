import json

import pandas as pd
import pytest

from catalog.ingest import UnsupportedFormatError, detect_format, infer_schema, preview


@pytest.fixture
def frame():
    return pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"], "amount": [1.5, 2.0, 3.25]})


def test_csv(tmp_path, frame):
    p = tmp_path / "x.csv"
    frame.to_csv(p, index=False)
    s = infer_schema(p)
    assert s.format == "csv"
    assert s.row_count == 3
    types = {c.name: c.data_type for c in s.columns}
    assert list(types) == ["id", "name", "amount"]
    assert types["id"] == "int64" and types["amount"] == "float64"
    assert types["name"] in ("object", "str", "string")  # pandas のバージョンで表記が異なる


def test_parquet(tmp_path, frame):
    p = tmp_path / "x.parquet"
    frame.to_parquet(p, index=False)
    s = infer_schema(p)
    assert s.format == "parquet"
    assert s.row_count == 3
    types = {c.name: c.data_type for c in s.columns}
    assert list(types) == ["id", "name", "amount"]
    assert types["id"] == "int64" and types["amount"] == "double"
    assert types["name"] in ("string", "large_string")


def test_json_and_jsonl(tmp_path):
    records = [{"k": 1, "v": "x", "nested": {"a": 1}}, {"k": 2, "v": "y", "nested": {"a": 2}}]
    pj = tmp_path / "x.json"
    pj.write_text(json.dumps(records))
    pl = tmp_path / "x.jsonl"
    pl.write_text("\n".join(json.dumps(r) for r in records))
    for p, fmt in [(pj, "json"), (pl, "jsonl")]:
        s = infer_schema(p)
        assert s.format == fmt
        assert s.row_count == 2
        assert [c.name for c in s.columns] == ["k", "v", "nested"]
        assert s.columns[2].data_type == "object"


def test_preview_limits_rows(tmp_path):
    p = tmp_path / "x.csv"
    pd.DataFrame({"i": range(100)}).to_csv(p, index=False)
    assert len(preview(p, n=20)) == 20


def test_unsupported(tmp_path):
    with pytest.raises(UnsupportedFormatError):
        detect_format(tmp_path / "x.xlsx")
    with pytest.raises(FileNotFoundError):
        infer_schema(tmp_path / "missing.csv")


def test_json_keeps_float_type(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"n": 1, "amount": 9200.0}\n{"n": 2, "amount": 10.0}\n')
    types = {c.name: c.data_type for c in infer_schema(p).columns}
    assert types == {"n": "int64", "amount": "float64"}


def test_parquet_preview_reads_only_first_batch(tmp_path, monkeypatch):
    """プレビューで Parquet 全体を読まない（PR #1 のレビュー指摘）。"""
    p = tmp_path / "big.parquet"
    pd.DataFrame({"i": range(1000), "s": [f"v{i}" for i in range(1000)]}).to_parquet(p, index=False, row_group_size=100)

    def full_read(*a, **kw):
        raise AssertionError("ファイル全体を読んでいる")

    monkeypatch.setattr(pd, "read_parquet", full_read)
    df = preview(p, n=20)
    assert list(df.columns) == ["i", "s"] and list(df["i"]) == list(range(20))


def test_parquet_preview_of_empty_file(tmp_path):
    p = tmp_path / "empty.parquet"
    pd.DataFrame({"i": pd.Series([], dtype="int64")}).to_parquet(p, index=False)
    df = preview(p)
    assert list(df.columns) == ["i"] and len(df) == 0


def test_jsonl_preview_reads_only_first_lines(tmp_path):
    """先頭の行だけを読むので、後ろに壊れた行があってもプレビューできる（全体を読んでいない証拠）。"""
    p = tmp_path / "big.jsonl"
    lines = [json.dumps({"k": i, "amount": 1.5}) for i in range(100)] + ["{broken"]
    p.write_text("\n".join(lines))
    df = preview(p, n=20)
    assert len(df) == 20 and list(df["k"]) == list(range(20)) and str(df["amount"].dtype) == "float64"
