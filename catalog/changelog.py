"""変更履歴の記録と参照。

書き込み関数は、自分のトランザクションの中で log() を呼ぶ。
こうすると、データの変更と履歴の記録が必ず一緒にコミット／ロールバックされる。
"""

from __future__ import annotations

import sqlite3

ENTITY_LABELS = {
    "asset": "アセット",
    "column": "カラム",
    "property": "プロパティ",
    "tag": "タグ",
    "lineage": "リネージ",
    "definition": "プロパティ定義",
    "user": "ユーザー",
    "domain": "ドメイン",
    "role": "役割",
}
ACTION_LABELS = {
    "create": "作成",
    "update": "変更",
    "delete": "削除",
    "add": "追加",
    "remove": "削除",
    "reingest": "データ更新",
    "rename": "名前変更",
}


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v)
    return str(v)


def log(
    conn: sqlite3.Connection,
    actor,
    entity: str,
    action: str,
    *,
    asset_id: int | None = None,
    asset_name: str | None = None,
    target: str | None = None,
    old=None,
    new=None,
) -> None:
    if asset_id is not None and asset_name is None:
        row = conn.execute("SELECT name FROM assets WHERE id = ?", (asset_id,)).fetchone()
        asset_name = row[0] if row else None
    conn.execute(
        "INSERT INTO change_log (user_id, username, asset_id, asset_name, entity, target, action, old_value, new_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            getattr(actor, "id", None),
            getattr(actor, "username", None),
            asset_id,
            asset_name,
            entity,
            target,
            action,
            _text(old),
            _text(new),
        ),
    )


def list_changes(
    conn: sqlite3.Connection,
    asset_id: int | None = None,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """新しい順の変更履歴。since / until は 'YYYY-MM-DD'（until はその日を含む）。"""
    sql = "SELECT * FROM change_log WHERE 1 = 1"
    params: list = []
    if asset_id is not None:
        sql += " AND asset_id = ?"
        params.append(asset_id)
    if username:
        sql += " AND username = ?"
        params.append(username)
    if since:
        sql += " AND at >= ?"
        params.append(since)
    if until:
        sql += " AND at < date(?, '+1 day')"
        params.append(until)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def last_update(conn: sqlite3.Connection, asset_id: int) -> dict | None:
    r = conn.execute(
        "SELECT username, at FROM change_log WHERE asset_id = ? ORDER BY id DESC LIMIT 1", (asset_id,)
    ).fetchone()
    return dict(r) if r else None


def to_display_rows(changes: list[dict], with_asset: bool = True) -> list[dict]:
    rows = []
    for c in changes:
        row = {"日時": c["at"][:19], "ユーザー": c["username"] or "（システム）"}
        if with_asset:
            row["アセット"] = c["asset_name"] or ""
        row.update(
            {
                "対象": ENTITY_LABELS.get(c["entity"], c["entity"]),
                "項目": c["target"] or "",
                "操作": ACTION_LABELS.get(c["action"], c["action"]),
                "変更前": c["old_value"] or "",
                "変更後": c["new_value"] or "",
            }
        )
        rows.append(row)
    return rows
