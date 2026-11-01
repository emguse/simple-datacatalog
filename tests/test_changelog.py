from catalog import changelog
from catalog import properties as props
from catalog import repository as repo
from catalog.ingest import ColumnSchema, FileSchema


def schema(*cols):
    return FileSchema("csv", 3, [ColumnSchema(c, "int64") for c in cols])


def entries(conn, asset_id=None):
    return [
        (c["username"], c["entity"], c["action"], c["target"], c["old_value"], c["new_value"])
        for c in reversed(changelog.list_changes(conn, asset_id=asset_id))
    ]


def test_asset_lifecycle_is_logged(conn, admin, editor):
    a = repo.register_asset(conn, admin, "orders", "o.csv", schema("cust", "amt")).asset_id
    b = repo.register_asset(conn, admin, "src", "s.csv", schema("x")).asset_id
    repo.update_asset_metadata(conn, editor, a, "説明1", add_tags=["sales"])
    repo.update_asset_metadata(conn, editor, a, "説明1")  # 変化なしは記録しない
    col = repo.list_columns(conn, a)[0]["id"]
    repo.update_column_metadata(conn, editor, col, "顧客", add_tags=["pii"])
    owner = props.get_builtin(conn, "source_system").id
    props.set_asset_properties(conn, editor, a, {owner: "山田"})
    props.set_asset_properties(conn, admin, a, {owner: "佐藤"})
    repo.apply_lineage_changes(conn, editor, a, add_up=[b])
    repo.reingest_asset(conn, admin, a, "o2.csv", schema("customer", "amt", "ch"), renames={"cust": "customer"})

    assert entries(conn, a) == [
        ("admin", "asset", "create", "o.csv", None, "csv, 3 行, 2 カラム"),
        ("editor", "asset", "update", "説明", "", "説明1"),
        ("editor", "tag", "add", "sales", None, None),
        ("editor", "column", "update", "cust / 説明", "", "顧客"),
        ("editor", "tag", "add", "cust / pii", None, None),
        ("editor", "property", "update", "ソースシステム", None, "山田"),
        ("admin", "property", "update", "ソースシステム", "山田", "佐藤"),  # 上書きされた値も追える
        ("editor", "lineage", "add", "src → orders", None, None),
        ("admin", "asset", "reingest", "o2.csv", "csv, 3 行", "csv, 3 行"),
        ("admin", "column", "rename", "customer", "cust", "customer"),
        ("admin", "column", "add", "ch", None, None),
    ]
    assert changelog.last_update(conn, a)["username"] == "admin"

    repo.delete_asset(conn, admin, a)
    last = changelog.list_changes(conn, limit=1)[0]
    assert (last["entity"], last["action"], last["asset_name"]) == ("asset", "delete", "orders")
    # アセットが消えても履歴は残る
    assert len(changelog.list_changes(conn, asset_id=a)) == 12


def test_filters_and_failed_write_not_logged(conn, admin, editor):
    a = repo.register_asset(conn, admin, "a", "a.csv", schema("x")).asset_id
    num = props.create_definition(conn, admin, "コスト", "number")
    owner = props.get_builtin(conn, "source_system").id
    assert props.set_asset_properties(conn, editor, a, {owner: "山田", num: "abc"})  # エラー → 何も保存しない
    assert [c["entity"] for c in changelog.list_changes(conn, asset_id=a)] == ["asset"]
    repo.update_asset_metadata(conn, editor, a, "d")
    assert {c["username"] for c in changelog.list_changes(conn, username="editor")} == {"editor"}
    assert changelog.list_changes(conn, since="2000-01-01", until="2000-01-02") == []
    rows = changelog.to_display_rows(changelog.list_changes(conn, asset_id=a))
    assert rows[0]["対象"] == "アセット" and rows[0]["操作"] == "変更"
