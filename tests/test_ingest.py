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
