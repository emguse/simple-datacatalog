"""ビジネスプロセス: 指標の起点となる業務の流れ（Kimball 流の「まずビジネスプロセスを決める」）。

ドメイン（オーナーを継承する単位）とは別の概念。名前と説明だけの軽い台帳で、指標が参照する。
"""

from __future__ import annotations

import sqlite3

from catalog import changelog
from catalog.auth import Perm, User, require
from catalog.tx import write_tx

FIELD_LABELS = {"name": "名前", "description": "説明"}


def _normalize(field: str, value) -> str:
    v = "" if value is None else str(value).strip()
    if field == "name" and not v:
        raise ValueError("ビジネスプロセスの名前を入力してください")
    return v


def list_business_processes(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM metrics m WHERE m.business_process_id = b.id) AS metric_count "
            "FROM business_processes b ORDER BY b.name"
        )
    ]


def get_business_process(conn: sqlite3.Connection, bp_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM business_processes WHERE id = ?", (bp_id,)).fetchone()
    return dict(r) if r else None


def create_business_process(conn: sqlite3.Connection, actor: User, name: str, description: str = "") -> int:
    require(actor, Perm.MANAGE_ASSETS)
    name = _normalize("name", name)
    description = _normalize("description", description)
    with write_tx(conn):
        try:
            cur = conn.execute(
                "INSERT INTO business_processes (name, description) VALUES (?, ?)", (name, description)
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"「{name}」はすでに登録されています") from None
        changelog.log(conn, actor, "business_process", "create", target=name, new=description or None)
    return cur.lastrowid


def update_business_process(conn: sqlite3.Connection, actor: User, bp_id: int, **changes) -> None:
    """渡された項目だけを変更する（項目単位のあと勝ち）。"""
    require(actor, Perm.MANAGE_ASSETS)
    bad = set(changes) - set(FIELD_LABELS)
    if bad:
        raise ValueError(f"変更できない項目です: {', '.join(sorted(bad))}")
    new = {f: _normalize(f, v) for f, v in changes.items()}
    with write_tx(conn):
        cur = get_business_process(conn, bp_id)
        if cur is None:
            raise ValueError("このビジネスプロセスは削除されています")
        diffs = {f: v for f, v in new.items() if v != cur[f]}
        if not diffs:
            return
        try:
            conn.execute(
                f"UPDATE business_processes SET {', '.join(f'{f} = ?' for f in diffs)} WHERE id = ?",
                (*diffs.values(), bp_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"「{diffs['name']}」はすでに登録されています") from None
        for f, v in diffs.items():
            changelog.log(conn, actor, "business_process", "update", target=f"{cur['name']} / {FIELD_LABELS[f]}",
                          old=cur[f], new=v)


def delete_business_process(conn: sqlite3.Connection, actor: User, bp_id: int) -> int:
    """削除する。紐づいていた指標は「未設定」に戻る（指標は消えない）。戻り値は未設定に戻った指標の数。"""
    require(actor, Perm.MANAGE_ASSETS)
    with write_tx(conn):
        cur = get_business_process(conn, bp_id)
        if cur is None:
            raise ValueError("このビジネスプロセスは削除されています")
        n = conn.execute("SELECT COUNT(*) FROM metrics WHERE business_process_id = ?", (bp_id,)).fetchone()[0]
        conn.execute("DELETE FROM business_processes WHERE id = ?", (bp_id,))
        changelog.log(conn, actor, "business_process", "delete", target=cur["name"], old=cur["description"] or None)
    return n
