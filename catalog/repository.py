"""assets / columns / tags / lineage の CRUD。

書き込み関数はすべて actor（操作するユーザー）を受け取り、権限を確認したうえで
write_tx の中で変更と変更履歴の記録を行う。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from catalog import changelog, lineage, properties, questions, roles
from catalog.asset_status import ACTIVE, ASSET_STATUSES, RETIRED
from catalog.auth import Perm, User, require
from catalog.ingest import FileSchema
from catalog.tx import write_tx


# ---------------------------------------------------------------- tags

def normalize_tags(names) -> list[str]:
    """前後の空白を除き、空と重複を取り除く（順序は保つ）。"""
    out: list[str] = []
    for n in names:
        n = str(n).strip()
        if n and n not in out:
            out.append(n)
    return out


def parse_tag_text(text: str | None) -> list[str]:
    return normalize_tags((text or "").split(","))


def _tag_ids(conn: sqlite3.Connection, names) -> list[int]:
    ids = []
    for n in normalize_tags(names):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (n,))
        ids.append(conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()[0])
    return ids


def _cleanup_unused_tags(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM asset_tags) "
        "AND id NOT IN (SELECT tag_id FROM column_tags)"
    )


def list_tags(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM tags ORDER BY name")]


# ---------------------------------------------------------------- assets

@dataclass
class RegisterResult:
    asset_id: int
    created: bool
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    renamed_columns: dict[str, str] = field(default_factory=dict)


def get_asset_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE name = ?", (name,)).fetchone()


def get_asset(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()


def _validate_renames(old_names: list[str], new_names: list[str], renames: dict[str, str]) -> None:
    targets = list(renames.values())
    if len(set(targets)) != len(targets):
        raise ValueError("同じ新カラムが複数の旧カラムに対応付けられています")
    for old, new in renames.items():
        if old not in old_names:
            raise ValueError(f"旧カラム {old} は存在しません")
        if new not in new_names:
            raise ValueError(f"新カラム {new} は新しいスキーマにありません")
        if new in old_names and new not in renames:
            raise ValueError(f"新カラム {new} は既存カラムと同じ名前です")


def diff_columns(
    conn: sqlite3.Connection, name: str, schema: FileSchema, renames: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """既存アセットに対して (追加されるカラム, 削除されるカラム) を返す。

    renames（旧名 → 新名）で対応付けたカラムは、追加・削除のどちらにも含めない。
    新規アセットなら全カラムが追加になる。
    """
    renames = renames or {}
    new_names = [c.name for c in schema.columns]
    row = get_asset_by_name(conn, name)
    if row is None:
        return new_names, []
    old_names = [r[0] for r in conn.execute("SELECT name FROM columns WHERE asset_id = ?", (row["id"],))]
    after_rename = {renames.get(n, n) for n in old_names}
    added = [n for n in new_names if n not in after_rename]
    removed = [n for n in old_names if n not in renames and n not in new_names]
    return added, removed


def _column_names(conn: sqlite3.Connection, asset_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM columns WHERE asset_id = ? ORDER BY ordinal", (asset_id,))]


def register_asset(
    conn: sqlite3.Connection,
    actor: User,
    name: str,
    file_path: str,
    schema: FileSchema,
    renames: dict[str, str] | None = None,
    domain_id: int | None = None,
) -> RegisterResult:
    """アセットを登録する。

    domain_id は新規登録のときだけ使う（再取込ではドメインを変えない。付け替えは roles.set_asset_domain）。

    同名のアセットが既にあれば再取込として扱い、アセット ID・説明・タグ・プロパティ・リネージ・ステータスを保つ。
    同じ名前のカラムと、renames（旧名 → 新名）で対応付けたカラムは説明・タグを引き継ぐ。
    差分の計算から書き込みまでを1つのトランザクションで行う。
    """
    require(actor, Perm.MANAGE_ASSETS)
    renames = {k: v for k, v in (renames or {}).items() if k != v}
    with write_tx(conn):
        row = get_asset_by_name(conn, name)
        created = row is None
        if created and renames:
            raise ValueError("新規アセットではカラムの対応付けは指定できません")
        if not created:
            _validate_renames(_column_names(conn, row["id"]), [c.name for c in schema.columns], renames)
        added, removed = diff_columns(conn, name, schema, renames)
        if created:
            if domain_id is not None and roles.get_domain(conn, domain_id) is None:
                raise ValueError("ドメインが削除されています")
            cur = conn.execute(
                "INSERT INTO assets (name, file_path, format, row_count, domain_id) VALUES (?, ?, ?, ?, ?)",
                (name, file_path, schema.format, schema.row_count, domain_id),
            )
            asset_id = cur.lastrowid
        else:
            asset_id = row["id"]
            conn.execute(
                "UPDATE assets SET file_path = ?, format = ?, row_count = ?, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?",
                (file_path, schema.format, schema.row_count, asset_id),
            )
            if removed:
                conn.execute(
                    f"DELETE FROM columns WHERE asset_id = ? AND name IN ({','.join('?' * len(removed))})",
                    (asset_id, *removed),
                )
                _cleanup_unused_tags(conn)
            # 名前の入れ替え（a→b, b→a）でも UNIQUE 制約に当たらないよう、一時名を経由する
            for old in renames:
                conn.execute(
                    "UPDATE columns SET name = ? WHERE asset_id = ? AND name = ?",
                    (f"\0rename\0{old}", asset_id, old),
                )
            for old, new in renames.items():
                conn.execute(
                    "UPDATE columns SET name = ? WHERE asset_id = ? AND name = ?",
                    (new, asset_id, f"\0rename\0{old}"),
                )
        for ordinal, col in enumerate(schema.columns):
            conn.execute(
                "INSERT INTO columns (asset_id, name, data_type, ordinal) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (asset_id, name) DO UPDATE SET data_type = excluded.data_type, "
                "ordinal = excluded.ordinal",
                (asset_id, col.name, col.data_type, ordinal),
            )

        if created:
            changelog.log(conn, actor, "asset", "create", asset_id=asset_id, target=file_path,
                          new=f"{schema.format}, {schema.row_count} 行, {len(schema.columns)} カラム")
        else:
            changelog.log(conn, actor, "asset", "reingest", asset_id=asset_id, target=file_path,
                          old=f"{row['format']}, {row['row_count']} 行",
                          new=f"{schema.format}, {schema.row_count} 行")
            for old, new in renames.items():
                changelog.log(conn, actor, "column", "rename", asset_id=asset_id, target=new, old=old, new=new)
            for c in added:
                changelog.log(conn, actor, "column", "add", asset_id=asset_id, target=c)
            for c in removed:
                changelog.log(conn, actor, "column", "remove", asset_id=asset_id, target=c)
    return RegisterResult(asset_id, created, added, removed, renames)


def reingest_asset(
    conn: sqlite3.Connection,
    actor: User,
    asset_id: int,
    file_path: str,
    schema: FileSchema,
    renames: dict[str, str] | None = None,
) -> RegisterResult:
    """既存アセットを ID 指定で更新する（メタデータは register_asset と同じルールで引き継ぐ）。

    ID から名前を引く確認と更新を1つのトランザクションで行う。分けると、その間に別のセッションが
    削除したアセットを新しい ID で作り直したり、同じ名前で登録し直された別のアセットを上書きしたりする。
    """
    require(actor, Perm.MANAGE_ASSETS)
    with write_tx(conn):  # register_asset の write_tx はこのトランザクションに相乗りする
        row = get_asset(conn, asset_id)
        if row is None:
            raise ValueError(f"アセット {asset_id} は存在しません")
        return register_asset(conn, actor, row["name"], file_path, schema, renames)


def assets_referencing_file(conn: sqlite3.Connection, path) -> list[str]:
    """このファイルを参照しているアセット名（パスは解決してから比べる）。

    アップロード先のファイルは、ローカルパス指定で別のアセットとしても登録できるため、
    置き換え前のファイルを消す前にこれで確かめる。
    """
    target = Path(path).resolve()
    return [
        r["name"]
        for r in conn.execute("SELECT name, file_path FROM assets ORDER BY name")
        if Path(r["file_path"]).resolve() == target
    ]


def delete_asset(conn: sqlite3.Connection, actor: User, asset_id: int) -> None:
    require(actor, Perm.MANAGE_ASSETS)
    with write_tx(conn):
        row = get_asset(conn, asset_id)
        if row is None:
            return
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        _cleanup_unused_tags(conn)
        changelog.log(conn, actor, "asset", "delete", asset_id=asset_id, asset_name=row["name"], target=row["file_path"])


def update_asset_metadata(
    conn: sqlite3.Connection,
    actor: User,
    asset_id: int,
    description: str | None = None,
    add_tags=(),
    remove_tags=(),
) -> None:
    """渡された項目だけを変更する（description=None なら説明は変えない。タグは追加・削除の差分で適用）。"""
    require(actor, Perm.EDIT)
    add_tags, remove_tags = normalize_tags(add_tags), normalize_tags(remove_tags)
    with write_tx(conn):
        row = get_asset(conn, asset_id)
        if row is None:
            raise ValueError("アセットが削除されています")
        changed = False
        if description is not None and description != row["description"]:
            conn.execute("UPDATE assets SET description = ? WHERE id = ?", (description, asset_id))
            changelog.log(conn, actor, "asset", "update", asset_id=asset_id, target="説明",
                          old=row["description"], new=description)
            changed = True
        current = set(asset_tags(conn, asset_id))
        for t in add_tags:
            if t in current:
                continue
            (tid,) = _tag_ids(conn, [t])
            conn.execute("INSERT INTO asset_tags (asset_id, tag_id) VALUES (?, ?)", (asset_id, tid))
            changelog.log(conn, actor, "tag", "add", asset_id=asset_id, target=t)
            changed = True
        for t in remove_tags:
            if t not in current:
                continue
            conn.execute(
                "DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
                (asset_id, t),
            )
            changelog.log(conn, actor, "tag", "remove", asset_id=asset_id, target=t)
            changed = True
        if changed:
            _cleanup_unused_tags(conn)
            conn.execute("UPDATE assets SET updated_at = datetime('now', 'localtime') WHERE id = ?", (asset_id,))


def set_asset_status(conn: sqlite3.Connection, actor: User, asset_id: int, status: str) -> None:
    """ステータスを変える。維持をやめる判断にあたるので、説明などの編集より強い権限を求める。"""
    require(actor, Perm.MANAGE_ASSETS)
    if status not in ASSET_STATUSES:
        raise ValueError(f"ステータスは {' / '.join(ASSET_STATUSES)} のいずれかです")
    with write_tx(conn):
        row = get_asset(conn, asset_id)
        if row is None:
            raise ValueError("アセットが削除されています")
        if row["status"] == status:
            return
        conn.execute(
            "UPDATE assets SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (status, asset_id),
        )
        changelog.log(conn, actor, "asset", "update", asset_id=asset_id, target="ステータス",
                      old=row["status"], new=status)


def non_active_statuses(conn: sqlite3.Connection) -> dict[int, str]:
    """運用中でないアセットの ID → ステータス。"""
    return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM assets WHERE status <> ?", (ACTIVE,))}


def status_warnings(conn: sqlite3.Connection, asset_id: int) -> list[str]:
    """ステータスに関わる依存関係の注意。

    - このアセットが運用中でないのに、有効な問いや下流のアセットが使っている
    - このアセットの上流（間接を含む）に、運用中でないアセットがある（廃止したアセット自身には出さない）
    """
    row = get_asset(conn, asset_id)
    if row is None:
        return []
    non_active = non_active_statuses(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM assets")}
    out = []
    if row["status"] != ACTIVE:
        if n := questions.active_question_counts(conn).get(asset_id):
            out.append(f"有効な問い {n} 件がこのデータを使っています。問いの見直しや、代わりのデータの案内が必要です。")
        users = [i for i in lineage.reachable(conn, asset_id, "down") if non_active.get(i) != RETIRED]
        if users:
            out.append("下流のデータがこのデータに依存しています: " + "、".join(sorted(names[i] for i in users)))
    if row["status"] != RETIRED:
        ups = [i for i in lineage.reachable(conn, asset_id, "up") if i in non_active]
        if ups:
            labels = sorted(f"{names[i]}（{non_active[i]}）" for i in ups)
            out.append("上流に運用中でないデータがあります: " + "、".join(labels))
    return out


def asset_tags(conn: sqlite3.Connection, asset_id: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "WHERE at.asset_id = ? ORDER BY t.name",
            (asset_id,),
        )
    ]


def list_assets(
    conn: sqlite3.Connection,
    query: str = "",
    tags=(),
    property_filters: dict[int, list[str]] | None = None,
    incomplete_only: bool = False,
    domain_ids=None,
    stale_roles_only: bool = False,
    no_questions_only: bool = False,
    statuses=(),
) -> list[dict]:
    """アセット一覧を絞り込んで返す。

    - query: 名前・説明・カラム名・カラム説明・プロパティ値の部分一致
    - tags: すべてのタグを含む（アセットまたはカラムのタグ）
    - property_filters: プロパティ ID → 値のリスト（項目間は AND、値の間は OR）
    - incomplete_only: 必須項目に未入力があるものだけ（廃止したものは入力を求めないので含めない）
    - statuses: ステータスのリスト。空なら絞り込まない
    - domain_ids: ドメイン ID のリスト（None を含めると「ドメインなし」も対象）。空なら絞り込まない
    - stale_roles_only: 最終確認が古い（要確認の）割り当てが効いているものだけ
    - no_questions_only: 答える（有効な）問いが1件もないものだけ（維持をやめる候補）

    キーワードは、ドメイン名・役割割り当て（組織・役職・担当者、ドメインからの継承分を含む）・
    このアセットを使う問いの文も対象にする（「粗利」で、粗利の問いに答えるアセットが見つかる）。
    """
    sql = "SELECT a.*, d.name AS domain_name FROM assets a LEFT JOIN domains d ON d.id = a.domain_id WHERE 1 = 1"
    params: list = []
    q = query.strip()
    if q:
        like = f"%{q}%"
        sql += (
            " AND (a.name LIKE ? OR a.description LIKE ? OR EXISTS ("
            "SELECT 1 FROM columns c WHERE c.asset_id = a.id AND (c.name LIKE ? OR c.description LIKE ?)) "
            "OR EXISTS (SELECT 1 FROM asset_properties ap WHERE ap.asset_id = a.id AND ap.value LIKE ?) "
            "OR d.name LIKE ? "
            "OR EXISTS (SELECT 1 FROM role_assignments ra WHERE (ra.asset_id = a.id OR ra.domain_id = a.domain_id) "
            "AND (ra.org LIKE ? OR ra.position LIKE ? OR ra.person LIKE ?)) "
            "OR EXISTS (SELECT 1 FROM question_assets qa JOIN questions q ON q.id = qa.question_id "
            "WHERE qa.asset_id = a.id AND q.text LIKE ?))"
        )
        params += [like] * 10
    for t in normalize_tags(tags):
        sql += (
            " AND (EXISTS (SELECT 1 FROM asset_tags at JOIN tags tg ON tg.id = at.tag_id "
            "WHERE at.asset_id = a.id AND tg.name = ?) OR EXISTS ("
            "SELECT 1 FROM columns c JOIN column_tags ct ON ct.column_id = c.id JOIN tags tg ON tg.id = ct.tag_id "
            "WHERE c.asset_id = a.id AND tg.name = ?))"
        )
        params += [t, t]
    for def_id, values in (property_filters or {}).items():
        values = [v for v in values if v]
        if not values:
            continue
        sql += (
            " AND EXISTS (SELECT 1 FROM asset_properties ap WHERE ap.asset_id = a.id AND ap.property_id = ? "
            f"AND ap.value IN ({','.join('?' * len(values))}))"
        )
        params += [def_id, *values]
    if domain_ids:
        ids = [i for i in domain_ids if i is not None]
        conds = []
        if ids:
            conds.append(f"a.domain_id IN ({','.join('?' * len(ids))})")
            params += ids
        if None in domain_ids:
            conds.append("a.domain_id IS NULL")
        sql += " AND (" + " OR ".join(conds) + ")"
    if statuses:
        sql += f" AND a.status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    sql += " ORDER BY a.name"
    defs = properties.list_definitions(conn)
    names = {d.id: d.name for d in defs}
    q_counts = questions.active_question_counts(conn)
    result = []
    for r in conn.execute(sql, params).fetchall():
        if no_questions_only and q_counts.get(r["id"]):
            continue
        comp = properties.completeness(conn, r["id"], defs)
        if incomplete_only and (comp.complete or r["status"] == RETIRED):
            continue
        effective = roles.effective_assignments(conn, r["id"])
        stale = sum(a["stale"] for a in effective)
        if stale_roles_only and not stale:
            continue
        d = dict(r)
        d["tags"] = asset_tags(conn, r["id"])
        d["column_count"] = conn.execute("SELECT COUNT(*) FROM columns WHERE asset_id = ?", (r["id"],)).fetchone()[0]
        d["completeness"] = comp
        d["properties"] = {names[k]: v for k, v in properties.get_asset_properties(conn, r["id"]).items()}
        d["last_update"] = changelog.last_update(conn, r["id"])
        d["owners"] = [roles.assignment_label(a) for a in effective if a["kind"] == "owner"]
        d["stewards"] = [f"{roles.assignment_label(a)}（{a['role_label']}）" for a in effective if a["kind"] == "steward"]
        d["stale_roles"] = stale
        d["question_count"] = q_counts.get(r["id"], 0)
        result.append(d)
    return result


# ---------------------------------------------------------------- columns

def list_columns(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM columns WHERE asset_id = ? ORDER BY ordinal", (asset_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = [
            t[0]
            for t in conn.execute(
                "SELECT t.name FROM column_tags ct JOIN tags t ON t.id = ct.tag_id "
                "WHERE ct.column_id = ? ORDER BY t.name",
                (r["id"],),
            )
        ]
        out.append(d)
    return out


def column_tags(conn: sqlite3.Connection, column_id: int) -> list[str]:
    return [
        t[0]
        for t in conn.execute(
            "SELECT t.name FROM column_tags ct JOIN tags t ON t.id = ct.tag_id WHERE ct.column_id = ? ORDER BY t.name",
            (column_id,),
        )
    ]


def update_column_metadata(
    conn: sqlite3.Connection,
    actor: User,
    column_id: int,
    description: str | None = None,
    add_tags=(),
    remove_tags=(),
) -> bool:
    """渡された項目だけを変更する。カラムが（再取込などで）消えていたら False。"""
    require(actor, Perm.EDIT)
    add_tags, remove_tags = normalize_tags(add_tags), normalize_tags(remove_tags)
    with write_tx(conn):
        col = conn.execute("SELECT * FROM columns WHERE id = ?", (column_id,)).fetchone()
        if col is None:
            return False
        aid, cname = col["asset_id"], col["name"]
        changed = False
        if description is not None and description != col["description"]:
            conn.execute("UPDATE columns SET description = ? WHERE id = ?", (description, column_id))
            changelog.log(conn, actor, "column", "update", asset_id=aid, target=f"{cname} / 説明",
                          old=col["description"], new=description)
            changed = True
        current = set(column_tags(conn, column_id))
        for t in add_tags:
            if t in current:
                continue
            (tid,) = _tag_ids(conn, [t])
            conn.execute("INSERT INTO column_tags (column_id, tag_id) VALUES (?, ?)", (column_id, tid))
            changelog.log(conn, actor, "tag", "add", asset_id=aid, target=f"{cname} / {t}")
            changed = True
        for t in remove_tags:
            if t not in current:
                continue
            conn.execute(
                "DELETE FROM column_tags WHERE column_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
                (column_id, t),
            )
            changelog.log(conn, actor, "tag", "remove", asset_id=aid, target=f"{cname} / {t}")
            changed = True
        if changed:
            _cleanup_unused_tags(conn)
            conn.execute("UPDATE assets SET updated_at = datetime('now', 'localtime') WHERE id = ?", (aid,))
    return True


# ---------------------------------------------------------------- lineage

def direct_upstreams(conn: sqlite3.Connection, asset_id: int) -> list[int]:
    return [r[0] for r in conn.execute("SELECT upstream_id FROM lineage WHERE downstream_id = ?", (asset_id,))]


def direct_downstreams(conn: sqlite3.Connection, asset_id: int) -> list[int]:
    return [r[0] for r in conn.execute("SELECT downstream_id FROM lineage WHERE upstream_id = ?", (asset_id,))]


def apply_lineage_changes(
    conn: sqlite3.Connection,
    actor: User,
    asset_id: int,
    add_up=(),
    remove_up=(),
    add_down=(),
    remove_down=(),
) -> list[str]:
    """直接の上流・下流を差分で変更する（他のユーザーが同時に追加・削除した関係は触らない）。

    循環チェックは同じトランザクションの中で行うので、同時に逆向きの関係を追加しても循環しない。
    循環などで追加できなかった関係のメッセージを返す（追加できたものは反映される）。
    """
    require(actor, Perm.EDIT)
    errors: list[str] = []
    with write_tx(conn):
        def name(i: int) -> str | None:
            row = get_asset(conn, i)
            return row["name"] if row else None

        me = name(asset_id)
        if me is None:
            raise ValueError("アセットが削除されています")
        edges = [(u, asset_id, True) for u in add_up] + [(asset_id, d, True) for d in add_down]
        edges += [(u, asset_id, False) for u in remove_up] + [(asset_id, d, False) for d in remove_down]
        for up, down, adding in sorted(edges, key=lambda e: e[2]):  # 削除を先に適用する
            up_name, down_name = name(up), name(down)
            label = f"{up_name} → {down_name}"
            if up_name is None or down_name is None:
                errors.append(f"{label}: アセットが削除されています")
                continue
            if not adding:
                if lineage.remove_edge(conn, up, down):
                    changelog.log(conn, actor, "lineage", "remove", asset_id=asset_id, target=label)
                continue
            try:
                if lineage.add_edge(conn, up, down):
                    changelog.log(conn, actor, "lineage", "add", asset_id=asset_id, target=label)
            except lineage.LineageCycleError as e:
                errors.append(f"{label}: {e}")
    return errors
