from catalog import repository as repo
from catalog.ingest import ColumnSchema, FileSchema


def schema(*cols):
    return FileSchema("csv", 10, [ColumnSchema(c, "int64") for c in cols])


def test_register_and_reingest_keeps_metadata(conn, admin):
    r = repo.register_asset(conn, admin, "orders", "/tmp/orders.csv", schema("id", "amount", "old"))
    assert r.created and r.added_columns == ["id", "amount", "old"]

    cols = {c["name"]: c for c in repo.list_columns(conn, r.asset_id)}
    repo.update_column_metadata(conn, admin, cols["id"]["id"], "注文ID", add_tags=["pk"])
    repo.update_column_metadata(conn, admin, cols["old"]["id"], "旧列", add_tags=["legacy"])

    assert repo.diff_columns(conn, "orders", schema("id", "amount", "new")) == (["new"], ["old"])
    r2 = repo.register_asset(conn, admin, "orders", "/tmp/orders.csv", schema("id", "amount", "new"))
    assert not r2.created and r2.asset_id == r.asset_id
    assert r2.added_columns == ["new"] and r2.removed_columns == ["old"]

    cols = {c["name"]: c for c in repo.list_columns(conn, r.asset_id)}
    assert list(cols) == ["id", "amount", "new"]
    assert cols["id"]["description"] == "注文ID" and cols["id"]["tags"] == ["pk"]
    # 削除されたカラムだけが使っていたタグは消える
    assert repo.list_tags(conn) == ["pk"]


def test_asset_metadata_and_tags(conn, admin):
    a = repo.register_asset(conn, admin, "a", "a.csv", schema("x")).asset_id
    repo.update_asset_metadata(conn, admin, a, "説明", add_tags=[" sales ", "pii", "sales", ""])
    assert repo.get_asset(conn, a)["description"] == "説明"
    assert repo.asset_tags(conn, a) == ["pii", "sales"]
    # 差分適用: 説明を渡さなければ変わらず、タグは追加・削除した分だけ変わる
    repo.update_asset_metadata(conn, admin, a, remove_tags=["pii"], add_tags=["new"])
    assert repo.get_asset(conn, a)["description"] == "説明"
    assert repo.asset_tags(conn, a) == ["new", "sales"]
    assert repo.list_tags(conn) == ["new", "sales"]  # 使われなくなったタグは消える


def test_parse_tag_text():
    assert repo.parse_tag_text(" a, b ,,a ") == ["a", "b"]
    assert repo.parse_tag_text(None) == []


def test_search(conn, admin):
    a = repo.register_asset(conn, admin, "raw_orders", "a.csv", schema("customer_id")).asset_id
    b = repo.register_asset(conn, admin, "daily_sales", "b.csv", schema("sales_date")).asset_id
    repo.update_asset_metadata(conn, admin, a, "生の注文データ", add_tags=["raw"])
    col = repo.list_columns(conn, b)[0]["id"]
    repo.update_column_metadata(conn, admin, col, "", add_tags=["pii"])

    names = lambda **kw: [x["name"] for x in repo.list_assets(conn, **kw)]
    assert names() == ["daily_sales", "raw_orders"]
    assert names(query="注文") == ["raw_orders"]
    assert names(query="sales_date") == ["daily_sales"]
    assert names(tags=["raw"]) == ["raw_orders"]
    assert names(tags=["pii"]) == ["daily_sales"]  # カラムのタグでもヒット
    assert names(tags=["raw", "pii"]) == []


def test_delete_cascades(conn, admin):
    a = repo.register_asset(conn, admin, "a", "a.csv", schema("x")).asset_id
    b = repo.register_asset(conn, admin, "b", "b.csv", schema("y")).asset_id
    repo.update_asset_metadata(conn, admin, a, "", add_tags=["t"])
    repo.apply_lineage_changes(conn, admin, b, add_up=[a])
    repo.delete_asset(conn, admin, a)
    assert conn.execute("SELECT COUNT(*) FROM columns WHERE asset_id = ?", (a,)).fetchone()[0] == 0
    assert repo.direct_upstreams(conn, b) == []
    assert repo.list_tags(conn) == []


def test_lineage_changes_report_cycles(conn, admin):
    a, b, c = (repo.register_asset(conn, admin, n, f"{n}.csv", schema("x")).asset_id for n in "abc")
    assert repo.apply_lineage_changes(conn, admin, b, add_up=[a], add_down=[c]) == []
    assert repo.direct_upstreams(conn, b) == [a]
    errors = repo.apply_lineage_changes(conn, admin, a, add_up=[c], add_down=[b])  # a→b は既存なので無視
    assert len(errors) == 1 and "c → a" in errors[0]
    assert repo.direct_upstreams(conn, a) == []
    repo.apply_lineage_changes(conn, admin, b, remove_up=[a])
    assert repo.direct_upstreams(conn, b) == []
    assert repo.direct_downstreams(conn, b) == [c]


def test_rename_carries_metadata(conn, admin):
    a = repo.register_asset(conn, admin, "orders", "o.csv", schema("cust_id", "amount")).asset_id
    cols = {c["name"]: c for c in repo.list_columns(conn, a)}
    repo.update_column_metadata(conn, admin, cols["cust_id"]["id"], "顧客ID", add_tags=["pii"])

    new = schema("customer_id", "amount", "channel")
    assert repo.diff_columns(conn, "orders", new) == (["customer_id", "channel"], ["cust_id"])
    assert repo.diff_columns(conn, "orders", new, {"cust_id": "customer_id"}) == (["channel"], [])

    r = repo.register_asset(conn, admin, "orders", "o.csv", new, renames={"cust_id": "customer_id"})
    assert r.renamed_columns == {"cust_id": "customer_id"}
    assert r.added_columns == ["channel"] and r.removed_columns == []
    after = {c["name"]: c for c in repo.list_columns(conn, a)}
    assert list(after) == ["customer_id", "amount", "channel"]
    assert after["customer_id"]["id"] == cols["cust_id"]["id"]
    assert after["customer_id"]["description"] == "顧客ID" and after["customer_id"]["tags"] == ["pii"]


def test_rename_swap(conn, admin):
    a = repo.register_asset(conn, admin, "t", "t.csv", schema("x", "y")).asset_id
    cols = {c["name"]: c["id"] for c in repo.list_columns(conn, a)}
    repo.register_asset(conn, admin, "t", "t.csv", schema("x", "y"), renames={"x": "y", "y": "x"})
    after = {c["name"]: c["id"] for c in repo.list_columns(conn, a)}
    assert after == {"x": cols["y"], "y": cols["x"]}


def test_invalid_renames(conn, admin):
    import pytest

    repo.register_asset(conn, admin, "t", "t.csv", schema("a", "b"))
    new = schema("c", "d")
    for bad in ({"zzz": "c"}, {"a": "zzz"}, {"a": "c", "b": "c"}):
        with pytest.raises(ValueError):
            repo.register_asset(conn, admin, "t", "t.csv", new, renames=bad)
    with pytest.raises(ValueError):  # 残っている既存カラム名への対応付け
        repo.register_asset(conn, admin, "t", "t.csv", schema("a", "b"), renames={"a": "b"})
    with pytest.raises(ValueError):
        repo.register_asset(conn, admin, "new_asset", "n.csv", new, renames={"a": "c"})
    assert [c["name"] for c in repo.list_columns(conn, repo.get_asset_by_name(conn, "t")["id"])] == ["a", "b"]


def test_reingest_keeps_asset_metadata_and_lineage(conn, admin):
    a = repo.register_asset(conn, admin, "a", "a.csv", schema("x")).asset_id
    b = repo.register_asset(conn, admin, "b", "b.csv", schema("y")).asset_id
    repo.update_asset_metadata(conn, admin, b, "説明", add_tags=["t"])
    repo.apply_lineage_changes(conn, admin, b, add_up=[a])
    r = repo.reingest_asset(conn, admin, b, "b2.parquet", FileSchema("parquet", 99, [ColumnSchema("y", "int64")]))
    assert r.asset_id == b and not r.created
    row = repo.get_asset(conn, b)
    assert (row["file_path"], row["format"], row["row_count"], row["description"]) == ("b2.parquet", "parquet", 99, "説明")
    assert repo.asset_tags(conn, b) == ["t"]
    assert repo.direct_upstreams(conn, b) == [a]
