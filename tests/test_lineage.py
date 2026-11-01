import pytest

from catalog import lineage
from catalog import repository as repo
from catalog.ingest import ColumnSchema, FileSchema


@pytest.fixture
def chain(conn, admin):
    s = FileSchema("csv", 1, [ColumnSchema("x", "int64")])
    ids = {n: repo.register_asset(conn, admin, n, f"{n}.csv", s).asset_id for n in ["src", "a", "b", "c", "other"]}
    # src -> a -> b -> c
    lineage.add_edge(conn, ids["src"], ids["a"])
    lineage.add_edge(conn, ids["a"], ids["b"])
    lineage.add_edge(conn, ids["b"], ids["c"])
    return ids


def test_reachable_with_depth(conn, admin, chain):
    assert lineage.reachable(conn, chain["a"], "down") == {chain["b"], chain["c"]}
    assert lineage.reachable(conn, chain["a"], "down", depth=1) == {chain["b"]}
    assert lineage.reachable(conn, chain["c"], "up", depth=2) == {chain["b"], chain["a"]}


def test_cycle_rejected(conn, admin, chain):
    with pytest.raises(lineage.LineageCycleError):
        lineage.add_edge(conn, chain["c"], chain["src"])
    with pytest.raises(lineage.LineageCycleError):
        lineage.add_edge(conn, chain["a"], chain["a"])
    lineage.add_edge(conn, chain["src"], chain["c"])  # 迂回路は循環ではない


def test_subgraph_and_dot(conn, admin, chain):
    g = lineage.subgraph(conn, chain["b"], depth=1)
    assert set(g.nodes) == {chain["a"], chain["b"], chain["c"]}
    assert g.edges == {(chain["a"], chain["b"]), (chain["b"], chain["c"])}
    dot = lineage.to_dot(g, chain["b"])
    assert "rankdir=LR" in dot
    assert f'n{chain["a"]} -> n{chain["b"]};' in dot
    assert f'n{chain["b"]} [label="b", fillcolor="#ffd966"' in dot
    assert "other" not in dot


def test_dot_escapes_quotes():
    g = lineage.Subgraph({1: 'we"ird'}, set())
    assert 'label="we\\"ird"' in lineage.to_dot(g, 1)
