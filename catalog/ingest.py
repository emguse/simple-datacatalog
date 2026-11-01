"""ファイルの形式判定・スキーマ推定・プレビュー。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

SUPPORTED_EXTENSIONS = {
    ".csv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}


@dataclass
class ColumnSchema:
    name: str
    data_type: str


@dataclass
class FileSchema:
    format: str
    row_count: int
    columns: list[ColumnSchema] = field(default_factory=list)


class UnsupportedFormatError(ValueError):
    pass


def detect_format(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    try:
        return SUPPORTED_EXTENSIONS[ext]
    except KeyError:
        raise UnsupportedFormatError(
            f"未対応の拡張子です: {ext or '(なし)'}（対応: {', '.join(sorted(SUPPORTED_EXTENSIONS))}）"
        ) from None


def _read_frame(path: Path, fmt: str, nrows: int | None = None) -> pd.DataFrame:
    if fmt == "csv":
        return pd.read_csv(path, nrows=nrows)
    if fmt == "parquet":
        df = pd.read_parquet(path)
        return df.head(nrows) if nrows is not None else df
    if fmt in ("json", "jsonl"):
        df = pd.read_json(path, lines=(fmt == "jsonl"), dtype=False)  # 値どおりの型を保つ（9200.0 を int にしない）
        return df.head(nrows) if nrows is not None else df
    raise UnsupportedFormatError(fmt)


def infer_schema(path: str | Path) -> FileSchema:
    """ファイルからカラム名・型・行数を取得する。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"ファイルが見つかりません: {path}")
    fmt = detect_format(path)

    if fmt == "parquet":
        pf = pq.ParquetFile(path)
        schema = pf.schema_arrow
        cols = [ColumnSchema(f.name, str(f.type)) for f in schema]
        return FileSchema(fmt, pf.metadata.num_rows, cols)

    df = _read_frame(path, fmt)
    cols = [ColumnSchema(str(name), str(dtype)) for name, dtype in df.dtypes.items()]
    return FileSchema(fmt, len(df), cols)


def preview(path: str | Path, n: int = 20) -> pd.DataFrame:
    path = Path(path)
    return _read_frame(path, detect_format(path), nrows=n)
