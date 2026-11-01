"""リネージのたどり・循環チェック・DOT 生成。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class LineageCycleError(ValueError):
    pass


def _neighbors(conn: sqlite3.Connection, asset_id: int, direction: str) -> list[int]:
    if direction == "up":
        sql = "SELECT upstream_id FROM lineage WHERE downstream_id = ?"
    else:
        sql = "SELECT downstream_id FROM lineage WHERE upstream_id = ?"
    return [r[0] for r in conn.execute(sql, (asset_id,))]


def reachable(conn: sqlite3.Connection, start_id: int, direction: str, depth: int | None = None) -> set[int]:
    """start_id から direction（'up' or 'down'）に depth 段までたどれるアセット ID（start 自身は含まない）。"""
    seen: set[int] = set()
    frontier = [start_id]
    level = 0
    while frontier and (depth is None or level < depth):
        nxt = []
        for node in frontier:
            for nb in _neighbors(conn, node, direction):
                if nb not in seen and nb != start_id:
                    seen.add(nb)
                    nxt.append(nb)
        frontier = nxt
        level += 1
    return seen


def add_edge(conn: sqlite3.Connection, upstream_id: int, downstream_id: int) -> bool:
    """upstream → downstream の辺を追加する。循環になる場合は LineageCycleError。

    コミットはしない。循環チェックと追加の間に他の書き込みが入らないよう、
    呼び出し側の write_tx の中で使うこと（repository.apply_lineage_changes）。
    追加したら True、既にあれば False。
    """
    if upstream_id == downstream_id:
        raise LineageCycleError("自分自身を上流・下流には指定できません")
    if upstream_id in reachable(conn, downstream_id, "down"):
        raise LineageCycleError("この関係を追加するとリネージが循環します")
    cur = conn.execute(
        "INSERT OR IGNORE INTO lineage (upstream_id, downstream_id) VALUES (?, ?)",
        (upstream_id, downstream_id),
    )
    return cur.rowcount > 0


def remove_edge(conn: sqlite3.Connection, upstream_id: int, downstream_id: int) -> bool:
    """辺を削除する（コミットはしない）。削除したら True。"""
    cur = conn.execute(
        "DELETE FROM lineage WHERE upstream_id = ? AND downstream_id = ?",
        (upstream_id, downstream_id),
    )
    return cur.rowcount > 0


@dataclass
class Subgraph:
    nodes: dict[int, str]  # id -> name
    edges: set[tuple[int, int]]  # (upstream, downstream)


def subgraph(conn: sqlite3.Connection, center_id: int, depth: int) -> Subgraph:
    """中心アセットから上流・下流それぞれ depth 段までの部分グラフ。"""
    ids = {center_id} | reachable(conn, center_id, "up", depth) | reachable(conn, center_id, "down", depth)
    placeholders = ",".join("?" * len(ids))
    nodes = {
        r["id"]: r["name"]
        for r in conn.execute(f"SELECT id, name FROM assets WHERE id IN ({placeholders})", tuple(ids))
    }
    edges = {
        (r[0], r[1])
        for r in conn.execute(
            f"SELECT upstream_id, downstream_id FROM lineage "
            f"WHERE upstream_id IN ({placeholders}) AND downstream_id IN ({placeholders})",
            tuple(ids) * 2,
        )
    }
    return Subgraph(nodes, edges)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(graph: Subgraph, center_id: int, notes: dict[int, str] | None = None) -> str:
    """notes: ノードのラベルの下に添える短い文字（例: 問いの数）"""
    notes = notes or {}
    lines = [
        "digraph lineage {",
        "  rankdir=LR;",
        '  node [shape=box, style="rounded,filled", fillcolor="#f5f5f5", fontname="sans-serif"];',
    ]
    for nid, name in sorted(graph.nodes.items()):
        label = f"{name}\n{notes[nid]}" if notes.get(nid) else name
        attrs = f"label={_quote(label)}"
        if nid == center_id:
            attrs += ', fillcolor="#ffd966", penwidth=2'
        lines.append(f"  n{nid} [{attrs}];")
    for up, down in sorted(graph.edges):
        lines.append(f"  n{up} -> n{down};")
    lines.append("}")
    return "\n".join(lines)
