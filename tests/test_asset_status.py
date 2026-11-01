import pytest

from catalog import changelog
from catalog import questions as qs
from catalog import repository as repo
from catalog.ingest import ColumnSchema, FileSchema


def schema(*cols):
    return FileSchema("csv", 1, [ColumnSchema(c, "int64") for c in cols])


@pytest.fixture
def assets(conn, admin):
    """原価 → 粗利 → 月次レポート のリネージを持つアセット。"""
    ids = {
        name: repo.register_asset(conn, admin, name, f"{name}.csv", schema("x")).asset_id
        for name in ("原価", "粗利", "月次レポート")
    }
    assert not repo.apply_lineage_changes(conn, admin, ids["粗利"], add_up=[ids["原価"]], add_down=[ids["月次レポート"]])
    return ids


def status_of(conn, asset_id):
    return repo.get_asset(conn, asset_id)["status"]


def test_new_asset_is_active(conn, assets):
    assert status_of(conn, assets["原価"]) == "運用中"
    assert repo.non_active_statuses(conn) == {}


def test_set_status_logs_only_real_changes(conn, admin, assets):
    aid = assets["原価"]
    repo.set_asset_status(conn, admin, aid, "廃止")
    repo.set_asset_status(conn, admin, aid, "廃止")
    assert status_of(conn, aid) == "廃止"
    logs = [c for c in changelog.list_changes(conn, asset_id=aid) if c["target"] == "ステータス"]
    assert [(c["old_value"], c["new_value"]) for c in logs] == [("運用中", "廃止")]


def test_set_status_rejects_unknown_value_and_missing_asset(conn, admin, assets):
    with pytest.raises(ValueError, match="ステータスは"):
        repo.set_asset_status(conn, admin, assets["原価"], "休止")
    with pytest.raises(ValueError, match="削除"):
        repo.set_asset_status(conn, admin, 9999, "廃止")


def test_set_status_requires_manage_assets(conn, power, editor, assets):
    with pytest.raises(PermissionError):
        repo.set_asset_status(conn, editor, assets["原価"], "廃止")
    repo.set_asset_status(conn, power, assets["原価"], "廃止")
    assert status_of(conn, assets["原価"]) == "廃止"


def test_reingest_keeps_status(conn, admin, assets):
    repo.set_asset_status(conn, admin, assets["原価"], "非推奨")
    repo.register_asset(conn, admin, "原価", "原価.csv", schema("x", "y"))
    assert status_of(conn, assets["原価"]) == "非推奨"


def test_list_assets_status_filter(conn, admin, assets):
    repo.set_asset_status(conn, admin, assets["原価"], "廃止")
    repo.set_asset_status(conn, admin, assets["粗利"], "非推奨")
    names = lambda **kw: [a["name"] for a in repo.list_assets(conn, **kw)]
    assert names() == ["原価", "月次レポート", "粗利"]
    assert names(statuses=["運用中", "非推奨"]) == ["月次レポート", "粗利"]
    assert names(statuses=["廃止"]) == ["原価"]


def test_incomplete_only_skips_retired(conn, admin, assets):
    repo.set_asset_status(conn, admin, assets["原価"], "廃止")
    names = [a["name"] for a in repo.list_assets(conn, incomplete_only=True)]
    assert "原価" not in names and "粗利" in names


def test_status_warnings_for_dependents(conn, admin, assets):
    qs.create_question(conn, admin, "粗利はいくらか？", asset_ids=[assets["粗利"]])
    assert repo.status_warnings(conn, assets["粗利"]) == []

    repo.set_asset_status(conn, admin, assets["粗利"], "非推奨")
    own = repo.status_warnings(conn, assets["粗利"])
    assert "有効な問い 1 件" in own[0]
    assert own[1].endswith("月次レポート")
    # 下流からは、間接の上流も含めて見える
    repo.set_asset_status(conn, admin, assets["原価"], "廃止")
    down = repo.status_warnings(conn, assets["月次レポート"])
    assert down == ["上流に運用中でないデータがあります: 原価（廃止）、粗利（非推奨）"]


def test_status_warnings_ignore_retired_downstream_and_upstream_of_retired(conn, admin, assets):
    repo.set_asset_status(conn, admin, assets["月次レポート"], "廃止")
    repo.set_asset_status(conn, admin, assets["粗利"], "廃止")
    # 粗利: 下流は廃止済みなので依存の注意なし
    assert repo.status_warnings(conn, assets["粗利"]) == []
    # 月次レポート: 自身が廃止なので、上流の注意は出さない
    assert repo.status_warnings(conn, assets["月次レポート"]) == []


def test_question_hint_for_non_active_asset(conn, admin, assets):
    qid = qs.create_question(conn, admin, "原価はいくらか？", asset_ids=[assets["原価"]])
    mention = lambda: [h for h in qs.hints(qs.get_question(conn, qid)) if "運用中でない" in h]
    assert mention() == []
    repo.set_asset_status(conn, admin, assets["原価"], "非推奨")
    assert "原価（非推奨）" in mention()[0]
    qs.update_question(conn, admin, qid, status="廃止")  # 廃止した問いには出さない
    assert mention() == []


def test_suggest_assets_excludes_retired(conn, admin, assets):
    suggested = lambda: {s["name"]: s["status"] for s in qs.suggest_assets(conn, "原価はいくらか")}
    assert suggested() == {"原価": "運用中"}
    repo.set_asset_status(conn, admin, assets["原価"], "非推奨")
    assert suggested() == {"原価": "非推奨"}
    repo.set_asset_status(conn, admin, assets["原価"], "廃止")
    assert suggested() == {}
