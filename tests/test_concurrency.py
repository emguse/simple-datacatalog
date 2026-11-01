"""同じ DB ファイルに複数の接続を張り、複数ユーザーの同時操作を再現する。"""

import threading

from catalog import lineage
from catalog import properties as props
from catalog import repository as repo
from catalog.db import connect
from catalog.ingest import ColumnSchema, FileSchema

S = FileSchema("csv", 1, [ColumnSchema("x", "int64")])


def test_different_fields_both_survive(conn, db_path, admin, editor):
    a = repo.register_asset(conn, admin, "a", "a.csv", S).asset_id
    src1 = repo.register_asset(conn, admin, "src1", "1.csv", S).asset_id
    src2 = repo.register_asset(conn, admin, "src2", "2.csv", S).asset_id
    c1, c2 = connect(db_path), connect(db_path)  # 2人のユーザーのセッション
    owner = props.get_builtin(conn, "source_system").id
    freq = props.get_builtin(conn, "update_frequency").id

    # どちらも「何も入っていない」画面を開いたあと、別々の項目を変更して保存する
    props.set_asset_properties(c1, admin, a, {owner: "山田"})
    props.set_asset_properties(c2, editor, a, {freq: "日次"})
    repo.update_asset_metadata(c1, admin, a, add_tags=["x"])
    repo.update_asset_metadata(c2, editor, a, add_tags=["y"])
    repo.apply_lineage_changes(c1, admin, a, add_up=[src1])
    repo.apply_lineage_changes(c2, editor, a, add_up=[src2])

    assert props.get_asset_properties(conn, a) == {owner: "山田", freq: "日次"}
    assert repo.asset_tags(conn, a) == ["x", "y"]
    assert sorted(repo.direct_upstreams(conn, a)) == [src1, src2]

    # 片方が上流を1つ外しても、もう一方が追加した上流は残る
    repo.apply_lineage_changes(c1, admin, a, remove_up=[src1])
    assert repo.direct_upstreams(conn, a) == [src2]


def test_same_field_last_writer_wins(conn, db_path, admin, editor):
    a = repo.register_asset(conn, admin, "a", "a.csv", S).asset_id
    c1, c2 = connect(db_path), connect(db_path)
    repo.update_asset_metadata(c1, admin, a, "Aの説明")
    repo.update_asset_metadata(c2, editor, a, "Bの説明")
    assert repo.get_asset(conn, a)["description"] == "Bの説明"


def _run_threads(n, target):
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def wrap(i):
        try:
            barrier.wait()
            target(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


def test_concurrent_opposite_edges_never_cycle(conn, db_path, admin):
    for trial in range(10):
        a = repo.register_asset(conn, admin, f"a{trial}", "a.csv", S).asset_id
        b = repo.register_asset(conn, admin, f"b{trial}", "b.csv", S).asset_id
        results = {}

        def work(i):
            c = connect(db_path)
            if i == 0:
                results[i] = repo.apply_lineage_changes(c, admin, a, add_down=[b])  # a → b
            else:
                results[i] = repo.apply_lineage_changes(c, admin, b, add_down=[a])  # b → a
            c.close()

        assert _run_threads(2, work) == []
        ok = [i for i, errs in results.items() if not errs]
        assert len(ok) == 1, results  # 片方だけ成功し、もう片方は循環エラー
        assert not (a in lineage.reachable(conn, a, "down"))


def test_concurrent_writes_all_applied(conn, db_path, admin):
    a = repo.register_asset(conn, admin, "a", "a.csv", S).asset_id
    defs = [props.create_definition(conn, admin, f"p{i}", "text") for i in range(8)]

    def work(i):
        c = connect(db_path)
        for k in range(5):
            assert props.set_asset_properties(c, admin, a, {defs[i]: f"v{i}-{k}"}) == []
            repo.update_asset_metadata(c, admin, a, add_tags=[f"t{i}-{k}"])
        c.close()

    assert _run_threads(8, work) == []
    values = props.get_asset_properties(conn, a)
    assert all(values[defs[i]] == f"v{i}-4" for i in range(8))
    assert len(repo.asset_tags(conn, a)) == 40
