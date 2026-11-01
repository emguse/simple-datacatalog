"""ドメインと役割割り当て（データオーナー／データスチュワードの2層）。

設計は docs/question-driven.md に従う。
- 役割割り当ては 1行1割り当て。役割の種類（role_types）はデータで持ち、3層化は行を足すだけで済む
- オーナーは役職で、スチュワードは人で持つ
- アセットはドメインの割り当てを継承し、例外だけを個別に上書きする
  - オーナーは「責任の範囲」ごとに継承する（アセット側で上書きした範囲だけを置き換える）
  - スチュワードは種類（業務／技術）ごとに継承する
- 継承元は保存せず、表示のたびに計算する
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from catalog import changelog
from catalog.auth import Perm, User, require
from catalog.tx import write_tx

OWNER_SCOPES = ["問いの受諾", "定義", "品質基準", "アクセス承認"]
STALE_DAYS = 183  # 最終確認から半年を過ぎたら「要確認」

SOURCE_OWN = "個別設定"
SOURCE_DOMAIN = "ドメインから継承"

FIELD_LABELS = {
    "org": "組織",
    "position": "役職",
    "person": "担当者",
    "scopes": "責任の範囲",
    "scope_note": "担当の範囲",
    "delegation_ref": "委譲規程",
    "contact": "問い合わせ窓口",
    "escalation": "エスカレーション先",
    "last_confirmed": "最終確認日",
}
# kind ごとの (項目, 必須)
KIND_FIELDS = {
    "owner": [
        ("org", True),
        ("position", True),
        ("person", False),
        ("scopes", True),
        ("delegation_ref", False),
        ("last_confirmed", True),
    ],
    "steward": [
        ("org", True),
        ("person", True),
        ("scope_note", False),
        ("contact", True),
        ("escalation", True),
        ("last_confirmed", True),
    ],
}
TEXT_FIELDS = ["org", "position", "person", "scope_note", "delegation_ref", "contact", "escalation"]


# ---------------------------------------------------------------- 役割の種類

def role_types(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM role_types ORDER BY ordinal, key")]


def _role_type(conn: sqlite3.Connection, key: str) -> dict:
    r = conn.execute("SELECT * FROM role_types WHERE key = ?", (key,)).fetchone()
    if r is None:
        raise ValueError(f"未対応の役割の種類です: {key}")
    return dict(r)


# ---------------------------------------------------------------- ドメイン

def list_domains(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM assets a WHERE a.domain_id = d.id) AS asset_count "
            "FROM domains d ORDER BY d.name"
        )
    ]


def get_domain(conn: sqlite3.Connection, domain_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    return dict(r) if r else None


def _check_domain_name(conn, name: str, exclude_id: int | None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("ドメイン名を入力してください")
    if conn.execute("SELECT 1 FROM domains WHERE name = ? AND id IS NOT ?", (name, exclude_id)).fetchone():
        raise ValueError(f"ドメイン「{name}」は既にあります")
    return name


def create_domain(conn: sqlite3.Connection, actor: User, name: str, description: str = "") -> int:
    require(actor, Perm.ADMIN)
    with write_tx(conn):
        name = _check_domain_name(conn, name, None)
        cur = conn.execute("INSERT INTO domains (name, description) VALUES (?, ?)", (name, description.strip()))
        changelog.log(conn, actor, "domain", "create", target=name, new=description.strip() or None)
    return cur.lastrowid


def update_domain(
    conn: sqlite3.Connection, actor: User, domain_id: int, name: str | None = None, description: str | None = None
) -> None:
    require(actor, Perm.ADMIN)
    with write_tx(conn):
        cur = get_domain(conn, domain_id)
        if cur is None:
            raise ValueError("ドメインが削除されています")
        if name is not None and name.strip() != cur["name"]:
            new = _check_domain_name(conn, name, domain_id)
            conn.execute("UPDATE domains SET name = ? WHERE id = ?", (new, domain_id))
            changelog.log(conn, actor, "domain", "update", target=f"{cur['name']} / 名前", old=cur["name"], new=new)
        if description is not None and description.strip() != cur["description"]:
            conn.execute("UPDATE domains SET description = ? WHERE id = ?", (description.strip(), domain_id))
            changelog.log(conn, actor, "domain", "update", target=f"{cur['name']} / 説明",
                          old=cur["description"], new=description.strip())


def delete_domain(conn: sqlite3.Connection, actor: User, domain_id: int) -> None:
    """ドメインを削除する。所属アセットはドメインなしになり、ドメインの割り当ては消える。"""
    require(actor, Perm.ADMIN)
    with write_tx(conn):
        cur = get_domain(conn, domain_id)
        if cur is None:
            return
        n_assets = conn.execute("SELECT COUNT(*) FROM assets WHERE domain_id = ?", (domain_id,)).fetchone()[0]
        n_roles = conn.execute("SELECT COUNT(*) FROM role_assignments WHERE domain_id = ?", (domain_id,)).fetchone()[0]
        conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
        changelog.log(conn, actor, "domain", "delete", target=cur["name"],
                      new=f"所属アセット {n_assets} 件はドメインなしに、割り当て {n_roles} 件は削除")


def set_asset_domain(conn: sqlite3.Connection, actor: User, asset_id: int, domain_id: int | None) -> None:
    require(actor, Perm.MANAGE_ASSETS)
    with write_tx(conn):
        row = conn.execute(
            "SELECT a.domain_id, d.name FROM assets a LEFT JOIN domains d ON d.id = a.domain_id WHERE a.id = ?",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError("アセットが削除されています")
        if row["domain_id"] == domain_id:
            return
        new_name = None
        if domain_id is not None:
            d = get_domain(conn, domain_id)
            if d is None:
                raise ValueError("ドメインが削除されています")
            new_name = d["name"]
        conn.execute(
            "UPDATE assets SET domain_id = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (domain_id, asset_id),
        )
        changelog.log(conn, actor, "asset", "update", asset_id=asset_id, target="ドメイン", old=row["name"], new=new_name)


# ---------------------------------------------------------------- 割り当て

def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["scopes"] = json.loads(d.get("scopes") or "[]")
    return d


def is_stale(last_confirmed: str, today: date | None = None) -> bool:
    today = today or date.today()
    try:
        return date.fromisoformat(last_confirmed) < today - timedelta(days=STALE_DAYS)
    except ValueError:
        return True


def assignment_label(a: dict) -> str:
    """一覧表示用の「誰か」。オーナーは役職、スチュワードは人。"""
    if a["kind"] == "owner":
        return f"{a['org']} / {a['position']}" + (f"（{a['person']}）" if a["person"] else "")
    return f"{a['org']} / {a['person']}"


def _validate(kind: str, values: dict) -> dict:
    """values（全項目）を検証・正規化して返す。"""
    out = dict(values)
    for f in TEXT_FIELDS:
        out[f] = str(out.get(f) or "").strip()
    scopes = [s for s in OWNER_SCOPES if s in (out.get("scopes") or [])] if kind == "owner" else []
    unknown = set(out.get("scopes") or []) - set(OWNER_SCOPES)
    if kind == "owner" and unknown:
        raise ValueError(f"未対応の責任範囲です: {', '.join(sorted(unknown))}")
    out["scopes"] = scopes
    lc = out.get("last_confirmed")
    if isinstance(lc, date):
        lc = lc.isoformat()
    try:
        out["last_confirmed"] = date.fromisoformat(str(lc or "")).isoformat()
    except ValueError:
        raise ValueError("最終確認日を YYYY-MM-DD 形式で入力してください") from None
    missing = [FIELD_LABELS[f] for f, required in KIND_FIELDS[kind] if required and not out.get(f)]
    if missing:
        raise ValueError("必須項目が未入力です: " + "、".join(missing))
    # 種類に関係ない項目は空にしておく
    used = {f for f, _ in KIND_FIELDS[kind]}
    for f in TEXT_FIELDS:
        if f not in used:
            out[f] = ""
    return out


def _check_duplicate(conn, kind: str, rt: str, domain_id, asset_id, v: dict, exclude_id: int | None) -> None:
    key_field = "position" if kind == "owner" else "person"
    dup = conn.execute(
        f"SELECT 1 FROM role_assignments WHERE role_type = ? AND domain_id IS ? AND asset_id IS ? "
        f"AND org = ? AND {key_field} = ? AND id IS NOT ?",
        (rt, domain_id, asset_id, v["org"], v[key_field], exclude_id),
    ).fetchone()
    if dup:
        raise ValueError(f"同じ{FIELD_LABELS['org']}・{FIELD_LABELS[key_field]}の割り当てが既にあります")


def _target_info(conn, domain_id, asset_id) -> tuple[int | None, str]:
    """変更履歴用: (asset_id, 対象の表示名)"""
    if asset_id is not None:
        return asset_id, ""
    d = get_domain(conn, domain_id)
    return None, f"ドメイン {d['name'] if d else domain_id} / "


def _require_target(actor: User, domain_id, asset_id) -> None:
    # ドメインの割り当ては管理者、アセットの割り当てはパワーユーザー以上
    require(actor, Perm.ADMIN if domain_id is not None else Perm.MANAGE_ASSETS)


def _summary(kind: str, v: dict) -> str:
    parts = []
    for f, _ in KIND_FIELDS[kind]:
        val = v.get(f)
        if f == "scopes":
            val = "・".join(val or [])
        if val:
            parts.append(f"{FIELD_LABELS[f]}={val}")
    return " ; ".join(parts)


def add_assignment(
    conn: sqlite3.Connection,
    actor: User,
    *,
    role_type: str,
    domain_id: int | None = None,
    asset_id: int | None = None,
    **fields,
) -> int:
    if (domain_id is None) == (asset_id is None):
        raise ValueError("割り当ての対象はドメインかアセットのどちらか一方です")
    _require_target(actor, domain_id, asset_id)
    with write_tx(conn):
        rt = _role_type(conn, role_type)
        if domain_id is not None and get_domain(conn, domain_id) is None:
            raise ValueError("ドメインが削除されています")
        if asset_id is not None and not conn.execute("SELECT 1 FROM assets WHERE id = ?", (asset_id,)).fetchone():
            raise ValueError("アセットが削除されています")
        v = _validate(rt["kind"], fields)
        _check_duplicate(conn, rt["kind"], role_type, domain_id, asset_id, v, None)
        cur = conn.execute(
            "INSERT INTO role_assignments (domain_id, asset_id, role_type, org, position, person, scopes, scope_note, "
            "delegation_ref, contact, escalation, last_confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                domain_id, asset_id, role_type, v["org"], v["position"], v["person"],
                json.dumps(v["scopes"], ensure_ascii=False), v["scope_note"], v["delegation_ref"],
                v["contact"], v["escalation"], v["last_confirmed"],
            ),
        )
        log_asset, prefix = _target_info(conn, domain_id, asset_id)
        changelog.log(conn, actor, "role", "add", asset_id=log_asset, target=f"{prefix}{rt['label']}",
                      new=_summary(rt["kind"], v))
    return cur.lastrowid


def get_assignment(conn: sqlite3.Connection, assignment_id: int) -> dict | None:
    r = conn.execute(
        "SELECT ra.*, rt.label AS role_label, rt.kind FROM role_assignments ra "
        "JOIN role_types rt ON rt.key = ra.role_type WHERE ra.id = ?",
        (assignment_id,),
    ).fetchone()
    return _row(r) if r else None


def update_assignment(conn: sqlite3.Connection, actor: User, assignment_id: int, **changes) -> None:
    """渡された項目だけを変更する（項目単位のあと勝ち）。役割の種類と対象は変えられない。"""
    with write_tx(conn):
        cur = get_assignment(conn, assignment_id)
        if cur is None:
            raise ValueError("この割り当ては削除されています")
        _require_target(actor, cur["domain_id"], cur["asset_id"])
        bad = set(changes) - set(FIELD_LABELS)
        if bad:
            raise ValueError(f"変更できない項目です: {', '.join(sorted(bad))}")
        v = _validate(cur["kind"], {**cur, **changes})
        _check_duplicate(conn, cur["kind"], cur["role_type"], cur["domain_id"], cur["asset_id"], v, assignment_id)
        diffs = [f for f in FIELD_LABELS if f in changes and v[f] != cur[f]]
        if not diffs:
            return
        conn.execute(
            f"UPDATE role_assignments SET {', '.join(f'{f} = ?' for f in diffs)}, "
            "updated_at = datetime('now', 'localtime') WHERE id = ?",
            (*[json.dumps(v[f], ensure_ascii=False) if f == "scopes" else v[f] for f in diffs], assignment_id),
        )
        log_asset, prefix = _target_info(conn, cur["domain_id"], cur["asset_id"])
        who = assignment_label(cur)
        for f in diffs:
            old, new = cur[f], v[f]
            if f == "scopes":
                old, new = "・".join(old), "・".join(new)
            changelog.log(conn, actor, "role", "update", asset_id=log_asset,
                          target=f"{prefix}{cur['role_label']}（{who}） / {FIELD_LABELS[f]}", old=old, new=new)


def confirm_assignment(conn: sqlite3.Connection, actor: User, assignment_id: int, today: date | None = None) -> None:
    """割り当てが正しいことを確認した日として、最終確認日を今日にする。"""
    update_assignment(conn, actor, assignment_id, last_confirmed=(today or date.today()).isoformat())


def delete_assignment(conn: sqlite3.Connection, actor: User, assignment_id: int) -> None:
    with write_tx(conn):
        cur = get_assignment(conn, assignment_id)
        if cur is None:
            return
        _require_target(actor, cur["domain_id"], cur["asset_id"])
        conn.execute("DELETE FROM role_assignments WHERE id = ?", (assignment_id,))
        log_asset, prefix = _target_info(conn, cur["domain_id"], cur["asset_id"])
        changelog.log(conn, actor, "role", "remove", asset_id=log_asset, target=f"{prefix}{cur['role_label']}",
                      old=_summary(cur["kind"], cur))


# ---------------------------------------------------------------- 参照（継承の計算）

_SELECT = (
    "SELECT ra.*, rt.label AS role_label, rt.kind, rt.ordinal AS role_ordinal FROM role_assignments ra "
    "JOIN role_types rt ON rt.key = ra.role_type "
)


def assignments_for(conn: sqlite3.Connection, *, domain_id: int | None = None, asset_id: int | None = None) -> list[dict]:
    """対象に直接付いている割り当て（継承なし）。"""
    col, val = ("domain_id", domain_id) if domain_id is not None else ("asset_id", asset_id)
    rows = conn.execute(_SELECT + f"WHERE ra.{col} = ? ORDER BY rt.ordinal, ra.org, ra.position, ra.person", (val,))
    return [_row(r) for r in rows]


def effective_assignments(conn: sqlite3.Connection, asset_id: int, today: date | None = None) -> list[dict]:
    """アセットに効いている割り当て。各行に source（個別設定／ドメインから継承）と stale を付ける。

    オーナー: ドメインのオーナーから、アセット側のオーナーが受け持つ範囲を除いたものを継承する。
    スチュワード: アセットにその種類の割り当てがなければ、ドメインのものを継承する。
    """
    row = conn.execute("SELECT domain_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return []
    own = assignments_for(conn, asset_id=asset_id)
    inherited = assignments_for(conn, domain_id=row["domain_id"]) if row["domain_id"] is not None else []
    domain = get_domain(conn, row["domain_id"]) if row["domain_id"] is not None else None

    result = [{**a, "source": SOURCE_OWN} for a in own]
    own_owner_scopes = {s for a in own if a["kind"] == "owner" for s in a["scopes"]}
    own_types = {a["role_type"] for a in own}
    for a in inherited:
        if a["kind"] == "owner":
            remaining = [s for s in a["scopes"] if s not in own_owner_scopes]
            if not remaining:
                continue
            a = {**a, "scopes": remaining}
        elif a["role_type"] in own_types:
            continue
        result.append({**a, "source": SOURCE_DOMAIN, "domain_name": domain["name"] if domain else ""})
    for a in result:
        a["stale"] = is_stale(a["last_confirmed"], today)
    result.sort(key=lambda a: (a["role_ordinal"], a["source"] != SOURCE_OWN, a["org"], a["position"], a["person"]))
    return result


def uncovered_owner_scopes(effective: list[dict]) -> list[str]:
    covered = {s for a in effective if a["kind"] == "owner" for s in a["scopes"]}
    return [s for s in OWNER_SCOPES if s not in covered]


def has_steward(effective: list[dict]) -> bool:
    return any(a["kind"] == "steward" for a in effective)


def list_all_assignments(
    conn: sqlite3.Connection,
    stale_only: bool = False,
    org: str | None = None,
    person: str | None = None,
    today: date | None = None,
) -> list[dict]:
    """棚卸し用: ドメイン・アセットの割り当て（継承前の実体）を横断して返す。"""
    sql = (
        _SELECT.replace("FROM role_assignments ra", ", d.name AS domain_name, a.name AS asset_name FROM role_assignments ra")
        + "LEFT JOIN domains d ON d.id = ra.domain_id LEFT JOIN assets a ON a.id = ra.asset_id WHERE 1 = 1"
    )
    params: list = []
    if org:
        sql += " AND ra.org = ?"
        params.append(org)
    if person:
        sql += " AND (ra.person = ? OR ra.position = ?)"
        params += [person, person]
    sql += " ORDER BY ra.last_confirmed, rt.ordinal, ra.id"
    out = []
    for r in conn.execute(sql, params):
        a = _row(r)
        a["stale"] = is_stale(a["last_confirmed"], today)
        if stale_only and not a["stale"]:
            continue
        out.append(a)
    return out


def suggestions(conn: sqlite3.Connection, field: str) -> list[str]:
    if field not in TEXT_FIELDS:
        raise ValueError(field)
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {field} FROM role_assignments WHERE {field} <> '' ORDER BY {field}"
    )]
