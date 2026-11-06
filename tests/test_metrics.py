import pytest

from catalog import business_processes as bps
from catalog import changelog
from catalog import metrics as ms
from catalog import questions as qs
from catalog import repository as repo
from catalog.db import connect
from catalog.ingest import ColumnSchema, FileSchema


def schema(*cols):
    return FileSchema("csv", 1, [ColumnSchema(c, "int64") for c in cols])


@pytest.fixture
def assets(conn, admin):
    return {
        n: repo.register_asset(conn, admin, n, f"{n}.csv", schema("x")).asset_id
        for n in ("受注明細", "粗利月次")
    }


def test_create_with_name_only(conn, editor):
    mid = ms.create_metric(conn, editor, "  売上高 ")
    m = ms.get_metric(conn, mid)
    assert (m["code"], m["name"], m["business_process_id"], m["assets"], m["questions"]) == (
        "M-001", "売上高", None, [], [])
    assert ms.get_metric(conn, ms.create_metric(conn, editor, "粗利率"))["code"] == "M-002"
    with pytest.raises(ValueError):
        ms.create_metric(conn, editor, " ")
    with pytest.raises(ValueError):
        ms.create_metric(conn, editor, "x", unknown="y")


def test_permissions(conn, editor, viewer):
    with pytest.raises(PermissionError):
        ms.create_metric(conn, viewer, "売上高")
    mid = ms.create_metric(conn, editor, "売上高")
    with pytest.raises(PermissionError):
        ms.update_metric(conn, viewer, mid, unit="円")
    with pytest.raises(PermissionError):
        ms.link_assets(conn, viewer, mid, add=[1])


def test_partial_update_history_and_bp_name(conn, power, editor):
    bid = bps.create_business_process(conn, power, "受注")
    mid = ms.create_metric(conn, editor, "売上高")
    ms.update_metric(conn, editor, mid, unit="円", business_process_id=bid)
    ms.update_metric(conn, editor, mid, business_process_id=0)  # 0 で未設定に戻す
    m = ms.get_metric(conn, mid)
    assert (m["unit"], m["business_process_id"]) == ("円", None)
    rows = [c for c in changelog.list_changes(conn) if c["entity"] == "metric" and "ビジネスプロセス" in (c["target"] or "")]
    assert [(r["old_value"], r["new_value"]) for r in reversed(rows)] == [(None, "受注"), ("受注", None)]
    n = len(changelog.list_changes(conn))
    ms.update_metric(conn, editor, mid, unit="円")  # 変更なし
    assert len(changelog.list_changes(conn)) == n
    with pytest.raises(ValueError):
        ms.update_metric(conn, editor, mid, business_process_id=9999)


def test_last_write_wins_per_field(conn, db_path, editor):
    mid = ms.create_metric(conn, editor, "売上高")
    other = connect(db_path)
    try:
        ms.update_metric(other, editor, mid, unit="円")
        ms.update_metric(conn, editor, mid, grain="月次")
    finally:
        other.close()
    m = ms.get_metric(conn, mid)
    assert (m["unit"], m["grain"]) == ("円", "月次")


def test_links_and_lookups(conn, admin, editor, assets):
    qid = qs.create_question(conn, editor, "月次の売上高はいくらか？")
    mid = ms.create_metric(conn, editor, "売上高", asset_ids=[assets["受注明細"]], question_ids=[qid])
    ms.link_assets(conn, editor, mid, add=[assets["粗利月次"]], remove=[assets["受注明細"]])
    assert [a["name"] for a in ms.get_metric(conn, mid)["assets"]] == ["粗利月次"]
    assert [m["id"] for m in ms.metrics_for_asset(conn, assets["粗利月次"])] == [mid]
    assert ms.metrics_for_asset(conn, assets["受注明細"]) == []
    assert [m["id"] for m in ms.metrics_for_question(conn, qid)] == [mid]
    ms.link_questions(conn, editor, mid, remove=[qid])
    assert ms.get_metric(conn, mid)["questions"] == []
    # アセットの履歴にも出る
    assert any(c["entity"] == "metric" for c in changelog.list_changes(conn, asset_id=assets["粗利月次"]))
    with pytest.raises(ValueError):
        ms.link_assets(conn, editor, mid, add=[9999])


def test_cascade_on_delete(conn, admin, editor, assets):
    mid = ms.create_metric(conn, editor, "売上高", asset_ids=[assets["受注明細"]])
    conn.execute("DELETE FROM assets WHERE id = ?", (assets["受注明細"],))
    assert ms.get_metric(conn, mid)["assets"] == []


def test_list_filters(conn, power, editor, assets):
    bid = bps.create_business_process(conn, power, "受注")
    a = ms.create_metric(conn, editor, "売上高", business_process_id=bid, asset_ids=[assets["受注明細"]],
                         formula="数量×単価")
    b = ms.create_metric(conn, editor, "粗利率")
    assert [m["id"] for m in ms.list_metrics(conn)] == [b, a]
    assert [m["id"] for m in ms.list_metrics(conn, query="単価")] == [a]
    assert [m["id"] for m in ms.list_metrics(conn, query="受注明細")] == [a]
    assert [m["id"] for m in ms.list_metrics(conn, business_process_id=bid)] == [a]
    assert [m["id"] for m in ms.list_metrics(conn, no_assets=True)] == [b]


def test_hints_and_suggestions(conn, power, editor, assets):
    mid = ms.create_metric(conn, editor, "売上高", unit="円", asset_ids=[assets["受注明細"]])
    assert len(ms.hints(ms.get_metric(conn, mid))) == 4  # BP・定義・計算式・粒度
    repo.set_asset_status(conn, power, assets["受注明細"], "廃止")
    assert any("運用中でない" in h for h in ms.hints(ms.get_metric(conn, mid)))
    ms.create_metric(conn, editor, "別", unit="円")
    assert ms.suggested_values(conn, "unit") == ["円"]
