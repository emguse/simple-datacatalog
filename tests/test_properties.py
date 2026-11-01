from datetime import date

import pytest

from catalog import properties as props
from catalog import repository as repo
from catalog import roles
from catalog.ingest import ColumnSchema, FileSchema


def schema(*cols):
    return FileSchema("csv", 1, [ColumnSchema(c, "int64") for c in cols])


def asset(conn, admin, name="a"):
    return repo.register_asset(conn, admin, name, f"{name}.csv", schema("x")).asset_id


def add_roles(conn, admin, asset_id):
    today = date.today()
    roles.add_assignment(conn, admin, asset_id=asset_id, role_type="owner", org="営業部", position="営業部長",
                         scopes=roles.OWNER_SCOPES, last_confirmed=today)
    roles.add_assignment(conn, admin, asset_id=asset_id, role_type="steward_business", org="営業管理課",
                         person="佐藤", contact="sato@example.com", escalation="営業部長", last_confirmed=today)


def test_builtins_seeded_once_and_not_overwritten(conn, admin):
    defs = props.list_definitions(conn)
    assert [d.builtin_key for d in defs] == [b[0] for b in props.BUILTIN_PROPERTIES]
    owner = props.get_builtin(conn, "source_system")
    props.update_definition(conn, admin, owner.id, "発生元", "text", required=False)
    props.seed_builtin(conn)
    props.seed_builtin(conn)
    assert len(props.list_definitions(conn)) == len(props.BUILTIN_PROPERTIES)
    owner = props.get_builtin(conn, "source_system")
    assert (owner.name, owner.required) == ("発生元", False)
    # オーナー・スチュワードはプロパティではなく役割割り当てで持つ
    assert not {"data_owner", "decision_owner", "data_steward"} & {d.builtin_key for d in defs}


def test_builtin_protection(conn, admin):
    vis = props.get_builtin(conn, "visibility")
    with pytest.raises(ValueError):
        props.delete_definition(conn, admin, vis.id)
    with pytest.raises(ValueError):
        props.update_definition(conn, admin, vis.id, vis.name, "text")
    # 選択肢・必須は変更できる
    props.update_definition(conn, admin, vis.id, vis.name, "select", ["社外公開", "全社公開"], required=False)
    assert props.get_definition(conn, vis.id).choices == ["社外公開", "全社公開"]


def test_custom_crud(conn, admin):
    a = asset(conn, admin)
    pid = props.create_definition(conn, admin, " コスト負担部署 ", "text", required=True)
    defs = props.list_definitions(conn)
    assert defs[-1].name == "コスト負担部署" and not defs[-1].builtin
    with pytest.raises(ValueError):
        props.create_definition(conn, admin, "コスト負担部署", "text")
    with pytest.raises(ValueError):
        props.create_definition(conn, admin, "区分", "select", [" ", ""])
    for reserved in ("説明", "オーナー", "スチュワード"):
        with pytest.raises(ValueError):
            props.create_definition(conn, admin, reserved, "text")

    assert props.set_asset_properties(conn, admin, a, {pid: "経理部"}) == []
    with pytest.raises(ValueError):  # 値があると型は変えられない
        props.update_definition(conn, admin, pid, "コスト負担部署", "number")
    props.update_definition(conn, admin, pid, "負担部署", "text", required=False, help="h")
    assert props.get_definition(conn, pid).name == "負担部署"

    props.delete_definition(conn, admin, pid)
    assert props.get_asset_properties(conn, a) == {}


@pytest.mark.parametrize(
    "type_,raw,expected",
    [
        ("text", "  abc ", "abc"),
        ("text", "  ", None),
        ("number", "1,000", "1000"),
        ("number", "1.50", "1.5"),
        ("number", "-3", "-3"),
        # float を通さないので精度が落ちない（PR #1 のレビュー指摘）
        ("number", "9007199254740993", "9007199254740993"),
        ("number", "12345678901234567890.123456789", "12345678901234567890.123456789"),
        ("number", "10.0", "10"),
        ("number", "1e3", "1000"),
        ("number", "1.5e-3", "0.0015"),
        ("number", "-0.0", "0"),
        ("date", "2026-11-01", "2026-11-01"),
        ("date", date(2026, 1, 2), "2026-01-02"),
        ("select", "日次", "日次"),
        ("url", "https://example.com/x", "https://example.com/x"),
        ("longtext", None, None),
    ],
)
def test_normalize_ok(type_, raw, expected):
    d = props.PropertyDef(1, "p", type_, choices=["日次"])
    assert props.normalize_value(d, raw) == expected


@pytest.mark.parametrize(
    "type_,raw",
    [("number", "abc"), ("number", "1-2"), ("number", "1e309"), ("number", "1e-100"), ("date", "2026/11/01"), ("select", "週次"), ("url", "example.com")],
)
def test_normalize_ng(type_, raw):
    d = props.PropertyDef(1, "p", type_, choices=["日次"])
    with pytest.raises(ValueError, match="^p: "):
        props.normalize_value(d, raw)


def test_set_properties_is_atomic(conn, admin):
    a = asset(conn, admin)
    owner = props.get_builtin(conn, "source_system").id
    freq = props.get_builtin(conn, "update_frequency").id
    errors = props.set_asset_properties(conn, admin, a, {owner: "山田", freq: "毎秒"})
    assert len(errors) == 1 and "更新頻度" in errors[0]
    assert props.get_asset_properties(conn, a) == {}
    assert props.set_asset_properties(conn, admin, a, {owner: "山田", freq: "日次"}) == []
    assert props.set_asset_properties(conn, admin, a, {owner: ""}) == []  # 空にすると削除
    assert props.get_asset_properties(conn, a) == {freq: "日次"}
    assert props.distinct_values(conn, freq) == ["日次"]


def test_completeness(conn, admin):
    a = asset(conn, admin)
    required = [d for d in props.list_definitions(conn) if d.required]
    c = props.completeness(conn, a)
    assert (c.filled, c.total) == (0, len(required) + 3)  # 説明・オーナー・スチュワードも数える
    assert c.missing[0] == "説明" and c.missing[-2:] == ["オーナー", "スチュワード"]

    repo.update_asset_metadata(conn, admin, a, "説明あり", add_tags=[])
    notes = props.get_builtin(conn, "usage_notes").id  # 任意項目は数えない
    values = {d.id: (d.choices[0] if d.type == "select" else "v") for d in required}
    assert props.set_asset_properties(conn, admin, a, {**values, notes: "注意"}) == []
    assert props.completeness(conn, a).missing == ["オーナー", "スチュワード"]
    add_roles(conn, admin, a)
    assert props.completeness(conn, a).complete

    props.create_definition(conn, admin, "新しい必須", "text", required=True)
    c = props.completeness(conn, a)
    assert not c.complete and c.missing == ["新しい必須"]


def test_list_assets_filters(conn, admin):
    a, b = asset(conn, admin, "a"), asset(conn, admin, "b")
    vis = props.get_builtin(conn, "visibility").id
    owner = props.get_builtin(conn, "source_system").id
    props.set_asset_properties(conn, admin, a, {vis: "全社公開", owner: "営業企画部"})
    props.set_asset_properties(conn, admin, b, {vis: "部門内"})

    names = lambda **kw: [x["name"] for x in repo.list_assets(conn, **kw)]
    assert names(property_filters={vis: ["全社公開"]}) == ["a"]
    assert names(property_filters={vis: ["全社公開", "部門内"]}) == ["a", "b"]
    assert names(property_filters={vis: ["部門内"], owner: ["営業企画部"]}) == []
    assert names(property_filters={vis: []}) == ["a", "b"]
    assert names(query="営業企画") == ["a"]

    res = {x["name"]: x for x in repo.list_assets(conn)}
    assert res["a"]["properties"]["公開範囲"] == "全社公開"
    assert res["a"]["completeness"].filled == 2

    required = [d for d in props.list_definitions(conn) if d.required]
    props.set_asset_properties(conn, admin, b, {d.id: (d.choices[0] if d.type == "select" else "v") for d in required})
    repo.update_asset_metadata(conn, admin, b, "説明", add_tags=[])
    add_roles(conn, admin, b)
    assert names(incomplete_only=True) == ["a"]


def test_properties_survive_reingest_and_cascade(conn, admin):
    a = asset(conn, admin)
    owner = props.get_builtin(conn, "source_system").id
    props.set_asset_properties(conn, admin, a, {owner: "山田"})
    repo.reingest_asset(conn, admin, a, "a2.csv", schema("x", "y"))
    assert props.get_asset_properties(conn, a) == {owner: "山田"}
    repo.delete_asset(conn, admin, a)
    assert conn.execute("SELECT COUNT(*) FROM asset_properties").fetchone()[0] == 0


def test_unchanged_value_kept_after_choice_removed(conn, admin):
    a = asset(conn, admin)
    freq = props.get_builtin(conn, "update_frequency")
    owner = props.get_builtin(conn, "source_system").id
    props.set_asset_properties(conn, admin, a, {freq.id: "毎時"})
    props.update_definition(conn, admin, freq.id, freq.name, "select", ["日次", "週次"], True)
    # 古い値のままなら他の項目を保存できる。新たに選択肢外の値を入れるとエラー
    assert props.set_asset_properties(conn, admin, a, {freq.id: "毎時", owner: "山田"}) == []
    assert props.get_asset_properties(conn, a)[freq.id] == "毎時"
    assert props.set_asset_properties(conn, admin, a, {freq.id: "年次"})
