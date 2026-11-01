"""アップロードファイルの staging と保存先（PR #1 のレビュー指摘への対応）。"""

from dataclasses import dataclass

import pytest

from catalog import uploads


@dataclass
class FakeUpload:
    name: str
    data: bytes
    file_id: str = ""

    def getbuffer(self):
        return memoryview(self.data)


def test_same_filename_from_two_sessions_does_not_collide(tmp_path):
    staging = tmp_path / "uploads" / ".staging"
    a = uploads.stage_upload(FakeUpload("orders.csv", b"a\n1\n", "file-a"), staging)
    b = uploads.stage_upload(FakeUpload("orders.csv", b"a\n2\n", "file-b"), staging)
    assert a != b and a.suffix == b.suffix == ".csv"
    assert (a.read_bytes(), b.read_bytes()) == (b"a\n1\n", b"a\n2\n")
    # 同じアップロードの再実行（rerun）は同じパス
    assert uploads.stage_upload(FakeUpload("orders.csv", b"a\n1\n", "file-a"), staging) == a


def test_unsafe_file_id_and_name_are_not_used_as_path(tmp_path):
    staging = tmp_path / "staging"
    p = uploads.stage_upload(FakeUpload("../../evil.csv", b"x", "../../etc"), staging)
    assert p.parent == staging and p.suffix == ".csv" and "etc" not in p.name
    with pytest.raises(ValueError):
        uploads.stage_upload(FakeUpload("payload.sh", b"x", "id"), staging)


@pytest.mark.parametrize("asset_name", ["../config", "/etc/passwd", "a/b", "..", "", "注文 明細"])
def test_store_upload_stays_inside_upload_dir(tmp_path, asset_name):
    upload_dir = tmp_path / "data" / "uploads"
    staged = uploads.stage_upload(FakeUpload("x.json", b"[]", "f1"), upload_dir / ".staging")
    dest = uploads.store_upload(staged, asset_name, upload_dir)
    assert dest.resolve().parent == upload_dir.resolve()
    assert dest.suffix == ".json" and dest.read_bytes() == b"[]" and not staged.exists()
    assert not (tmp_path / "data" / "config.json").exists()


def test_store_upload_names_are_unique_and_readable(tmp_path):
    upload_dir = tmp_path / "uploads"
    d1 = uploads.store_upload(uploads.stage_upload(FakeUpload("a.csv", b"1", "f1"), tmp_path / "s"), "raw_orders", upload_dir)
    d2 = uploads.store_upload(uploads.stage_upload(FakeUpload("a.csv", b"2", "f2"), tmp_path / "s"), "raw_orders", upload_dir)
    assert d1 != d2 and d1.name.startswith("raw_orders-") and d2.read_bytes() == b"2"


def test_discard_only_inside_upload_dir(tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    inside = upload_dir / "old.csv"
    inside.write_text("x")
    outside = tmp_path / "local.csv"
    outside.write_text("x")
    nested = upload_dir / ".staging" / "s.csv"
    nested.parent.mkdir()
    nested.write_text("x")

    assert not uploads.discard_upload(outside, upload_dir)          # ローカルパス参照の元ファイル
    assert not uploads.discard_upload(nested, upload_dir)           # 直下でないもの
    assert not uploads.discard_upload(upload_dir / "../local.csv", upload_dir)
    assert not uploads.discard_upload(inside, upload_dir, keep=inside)
    assert not uploads.discard_upload(None, upload_dir)
    assert uploads.discard_upload(inside, upload_dir)
    assert outside.exists() and nested.exists() and not inside.exists()
