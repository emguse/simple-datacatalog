"""アップロードされたファイルの一時置き場（staging）と保存先（uploads/）の管理。

- staging のファイル名はアップロードごとに一意にする（セッション間で同じファイル名がぶつからないように）
- 保存先のファイル名はアセット名から安全な文字だけを取り出し、一意な接尾辞を付けて作る。
  解決後のパスが必ず upload_dir の直下にあることを確かめる（パストラバーサル対策）
- 置き換え前のファイルの削除は、DB の更新が成功した「後」に discard_upload で行う
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from catalog.ingest import SUPPORTED_EXTENSIONS

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_CHARS = re.compile(r"[^\w-]+", re.UNICODE)


def _suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"未対応の拡張子です: {suffix or '(なし)'}")
    return suffix


def _inside(path: Path, directory: Path) -> bool:
    return path.resolve().parent == directory.resolve()


def stage_upload(uploaded, staging_dir: Path) -> Path:
    """アップロードされたファイルを staging に書き出し、そのパスを返す。

    ファイル名はアップロードごとに一意（Streamlit の file_id、なければ乱数）で、元の拡張子だけを引き継ぐ。
    同じアップロードの再実行（rerun）では同じパスに上書きする。
    """
    suffix = _suffix(getattr(uploaded, "name", ""))
    file_id = str(getattr(uploaded, "file_id", "") or "")
    stem = file_id if _SAFE_ID.match(file_id) else uuid.uuid4().hex
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"{stem}{suffix}"
    if not _inside(staged, staging_dir):  # 念のため
        raise ValueError("staging のパスが不正です")
    staged.write_bytes(uploaded.getbuffer())
    return staged


def storage_name(asset_name: str, suffix: str) -> str:
    """保存用のファイル名: アセット名の安全な部分 + 一意な接尾辞 + 拡張子。"""
    base = _UNSAFE_CHARS.sub("_", asset_name).strip("._-")[:50] or "asset"
    return f"{base}-{uuid.uuid4().hex[:12]}{suffix}"


def store_upload(staged: Path, asset_name: str, upload_dir: Path) -> Path:
    """staging のファイルを upload_dir に移し、保存先のパスを返す。古いファイルはここでは消さない。"""
    suffix = _suffix(staged.name)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / storage_name(asset_name, suffix)
    if not _inside(dest, upload_dir):
        raise ValueError("保存先のパスが不正です")
    shutil.move(staged, dest)
    return dest


def discard_upload(path: str | Path | None, upload_dir: Path, keep: str | Path | None = None) -> bool:
    """upload_dir の直下にあるファイルだけを削除する（ローカルパス参照の元ファイルや keep には触らない）。"""
    if not path:
        return False
    p = Path(path).resolve()
    if keep is not None and p == Path(keep).resolve():
        return False
    if not _inside(p, upload_dir) or not p.is_file():
        return False
    p.unlink()
    return True
