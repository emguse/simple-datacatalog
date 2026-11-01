from datetime import date, timedelta

import pytest

from catalog import changelog
from catalog import questions as qs
from catalog import repository as repo
from catalog import roles
from catalog.db import connect
from catalog.ingest import ColumnSchema, FileSchema

TODAY = date(2026, 11, 1)


def schema(*cols):
    return FileSchema("csv", 1, [ColumnSchema(c, "int64") for c in cols])


@pytest.fixture
def assets(conn, admin):
    ids = {}
    for name, desc, cols in [
        ("受注明細", "得意先ごとの受注の明細。単価・数量を含む", ("customer_id", "unit_price")),
        ("原価", "商品ごとの原価", ("product", "cost")),
        ("出荷指示", "出荷の指示と実績", ("order_id", "shipped_at")),
        ("粗利月次", "得意先別の月次粗利の集計", ("month", "gross_profit")),
    ]:
        ids[name] = repo.register_asset(conn, admin, name, f"{name}.csv", schema(*cols)).asset_id
        repo.update_asset_metadata(conn, admin, ids[name], desc)
    return ids


def test_create_with_text_only(conn, editor):
    qid = qs.create_question(conn, editor, "  得意先別の月次粗利はいくらか？ ")
    q = qs.get_question(conn, qid)
    assert (q["code"], q["text"], q["decision"], q["status"], q["requester"]) == (
        "Q-001", "得意先別の月次粗利はいくらか？", "申請中", "有効", "editor")
    assert q["assets"] == []
    assert qs.get_question(conn, qs.create_question(conn, editor, "二つ目"))["code"] == "Q-002"
    with pytest.raises(ValueError):
        qs.create_question(conn, editor, "  ")
    with pytest.raises(ValueError):
        qs.create_question(conn, editor, "x", kind="その他")
    with pytest.raises(ValueError):
        qs.create_question(conn, editor, "x", review_date="来年")


def test_permissions(conn, viewer, editor):
    with pytest.raises(PermissionError):
        qs.create_question(conn, viewer, "見るだけの人は登録できない？")
    qid = qs.create_question(conn, editor, "編集者は登録できるか？")
    with pytest.raises(PermissionError):
        qs.update_question(conn, viewer, qid, kind="分析")
    with pytest.raises(PermissionError):
        qs.link_assets(conn, viewer, qid, add=[])


def test_partial_update_and_history(conn, db_path, admin, editor):
    qid = qs.create_question(conn, editor, "今月の受注見込はどれくらいか？")
    c1, c2 = connect(db_path), connect(db_path)
    qs.update_question(c1, editor, qid, required_quality="受注入力が受領から1営業日以内")
    qs.update_question(c2, admin, qid, kind="分析")
    q = qs.get_question(conn, qid)
    assert (q["required_quality"], q["kind"]) == ("受注入力が受領から1営業日以内", "分析")
    qs.update_question(conn, admin, qid, kind="分析")  # 変化なしは記録しない
    rows = [(c["username"], c["target"]) for c in reversed(changelog.list_changes(conn)) if c["entity"] == "question"]
    assert rows == [("editor", "Q-001"), ("editor", "Q-001 / 必要な品質"), ("admin", "Q-001 / 種別")]


def test_accept_fills_decided_at(conn, admin):
    qid = qs.create_question(conn, admin, "この注文は出荷済みか？")
    qs.update_question(conn, admin, qid, decision="受諾")
    assert qs.get_question(conn, qid)["decided_at"] == date.today().isoformat()
    qid2 = qs.create_question(conn, admin, "x", decision="条件付き受諾", decided_at="2026-01-05")
    assert qs.get_question(conn, qid2)["decided_at"] == "2026-01-05"


def test_link_assets(conn, admin, assets):
    qid = qs.create_question(conn, admin, "得意先別の月次粗利はいくらか？", asset_ids=[assets["受注明細"]])
    qs.link_assets(conn, admin, qid, add=[assets["原価"], assets["受注明細"]], remove=[])
    assert [a["name"] for a in qs.get_question(conn, qid)["assets"]] == ["原価", "受注明細"]
    qs.link_assets(conn, admin, qid, remove=[assets["原価"]])
    assert [a["name"] for a in qs.get_question(conn, qid)["assets"]] == ["受注明細"]
    # アセットの履歴にも残る
    hist = changelog.list_changes(conn, asset_id=assets["原価"])
    assert [(c["entity"], c["action"]) for c in hist[:2]] == [("question", "remove"), ("question", "add")]
    repo.delete_asset(conn, admin, assets["受注明細"])
    assert qs.get_question(conn, qid)["assets"] == []
    with pytest.raises(ValueError):
        qs.link_assets(conn, admin, qid, add=[99999])


@pytest.mark.parametrize("text,ok", [
    ("得意先別の月次粗利はいくらか？", True), ("出荷済みか", True), ("出荷済みか。", True),
    ("Is it shipped?", True), ("売上はどうなっていますか", True), ("月次粗利", False), ("粗利を出す", False),
])
def test_looks_like_question(text, ok):
    assert qs.looks_like_question(text) is ok


def test_hints_grow_with_question(conn, admin, assets):
    qid = qs.create_question(conn, admin, "月次粗利")
    h = qs.hints(qs.get_question(conn, qid), TODAY)
    assert len(h) == 3 and h[0].startswith("疑問文") and "データを紐づける" in h[1] and "必要な品質" in h[2]

    qs.update_question(conn, admin, qid, text="得意先別の月次粗利はいくらか？", required_quality="月末締めの翌3営業日までに確定")
    qs.link_assets(conn, admin, qid, add=[assets["受注明細"]])
    assert qs.hints(qs.get_question(conn, qid), TODAY) == []

    qs.update_question(conn, admin, qid, decision="条件付き受諾")
    h = qs.hints(qs.get_question(conn, qid), TODAY)
    assert [x[:6] for x in h] == ["受諾を判断し", "受諾済みです", "条件付き受諾", "見直し日を決"]

    qs.update_question(conn, admin, qid, decided_by="営業部 営業部長", monitor="営業管理課 佐藤",
                       condition_note="入力遅延の解消後", review_date=(TODAY - timedelta(days=1)).isoformat())
    assert qs.hints(qs.get_question(conn, qid), TODAY) == [
        "見直し日を過ぎています。この問いはまだ有効ですか？ 必要な品質は変わっていませんか？"]
    assert [q["id"] for q in qs.list_questions(conn, review_due=True, today=TODAY)] == [qid]
    qs.update_question(conn, admin, qid, status="廃止")
    assert qs.hints(qs.get_question(conn, qid), TODAY) == []
    assert qs.list_questions(conn, review_due=True, today=TODAY) == []


def test_suggest_assets_and_similar(conn, admin, assets):
    top = qs.suggest_assets(conn, "得意先別の月次粗利はいくらか？")
    assert top[0]["name"] == "粗利月次" and "粗利" in top[0]["matched"]
    assert "受注明細" in [a["name"] for a in top]  # 「得意先」で当たる
    assert "出荷指示" not in [a["name"] for a in top]
    assert qs.suggest_assets(conn, "のはか？") == []  # 助詞だけでは候補を出さない

    qs.create_question(conn, admin, "得意先別の月次粗利はいくらか？")
    qs.create_question(conn, admin, "この注文は出荷済みか？")
    sim = qs.similar_questions(conn, "得意先ごとの月次の粗利は？")
    assert [q["code"] for q in sim] == ["Q-001"]
    assert qs.similar_questions(conn, "在庫は足りているか？") == []


def test_suggested_deciders_and_monitors(conn, admin, assets):
    d = roles.create_domain(conn, admin, "販売管理")
    roles.set_asset_domain(conn, admin, assets["受注明細"], d)
    roles.add_assignment(conn, admin, domain_id=d, role_type="owner", org="営業部", position="営業部長",
                         scopes=roles.OWNER_SCOPES, last_confirmed=TODAY)
    roles.add_assignment(conn, admin, asset_id=assets["原価"], role_type="owner", org="経理部", position="経理部長",
                         scopes=["定義"], last_confirmed=TODAY)  # 問いの受諾は持たない
    roles.add_assignment(conn, admin, asset_id=assets["受注明細"], role_type="steward_business", org="営業管理課",
                         person="佐藤", contact="x", escalation="y", last_confirmed=TODAY)
    ids = [assets["受注明細"], assets["原価"]]
    assert qs.suggested_deciders(conn, ids) == ["営業部 営業部長"]
    assert qs.suggested_monitors(conn, ids) == ["営業管理課 佐藤"]


def test_downstream_questions(conn, admin, assets):
    repo.apply_lineage_changes(conn, admin, assets["粗利月次"], add_up=[assets["受注明細"], assets["原価"]])
    q1 = qs.create_question(conn, admin, "得意先別の月次粗利はいくらか？", asset_ids=[assets["粗利月次"]])
    qs.create_question(conn, admin, "原価の更新漏れはないか？", asset_ids=[assets["原価"]])
    q3 = qs.create_question(conn, admin, "廃止した問い", asset_ids=[assets["粗利月次"]], status="廃止")
    down = qs.downstream_questions(conn, assets["原価"])
    assert [(q["id"], q["via"]) for q in down] == [(q1, ["粗利月次"])]
    assert q3 not in [q["id"] for q in down]
    assert qs.downstream_questions(conn, assets["粗利月次"]) == []


def test_list_assets_question_counts(conn, admin, assets):
    qs.create_question(conn, admin, "得意先別の月次粗利はいくらか？", asset_ids=[assets["粗利月次"], assets["受注明細"]])
    qs.create_question(conn, admin, "古い問い", asset_ids=[assets["原価"]], status="廃止")
    res = {a["name"]: a["question_count"] for a in repo.list_assets(conn)}
    assert res == {"受注明細": 1, "原価": 0, "出荷指示": 0, "粗利月次": 1}
    names = lambda **kw: [a["name"] for a in repo.list_assets(conn, **kw)]
    assert names(no_questions_only=True) == ["出荷指示", "原価"]
    assert names(query="いくらか") == ["受注明細", "粗利月次"]  # 問いの文でヒット
    assert [q["code"] for q in qs.list_questions(conn, no_assets=True)] == []
    assert len(qs.list_questions(conn, query="粗利月次")) == 1  # アセット名でも問いを探せる
