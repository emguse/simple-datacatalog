"""指標（メトリクス）台帳: ビジネスプロセスから導く、数字の定義。

- 登録に必須なのは名前だけ。ほかは後から育てる（足りないところは hints() で示す。エラーにはしない）
- 計算式は文章で持つ。カラム単位の計算式は、これまでどおりアセット／カラムの説明に書く
- 元データ（アセット）と、答える問いに多対多で紐づける
"""

from __future__ import annotations

import sqlite3

from catalog import changelog
from catalog.asset_status import ACTIVE
from catalog.auth import Perm, User, require
from catalog.tx import write_tx

FIELD_LABELS = {
    "name": "名前",
    "business_process_id": "ビジネスプロセス",
    "description": "定義",
    "formula": "計算式",
    "unit": "単位",
    "grain": "集計粒度",
}

EXAMPLES = ["売上高（月次）", "粗利率", "出荷リードタイム"]


# ---------------------------------------------------------------- 検証

def _normalize(field: str, value):
    if field == "business_process_id":
        # 「未設定」は 0 / None / 空文字のどれでも None にそろえる
        return int(value) if value not in (None, "", 0) else None
    v = "" if value is None else str(value).strip()
    if field == "name" and not v:
        raise ValueError("指標の名前を入力してください")
    return v


def _check_bp(conn, bp_id: int | None) -> str | None:
    """ビジネスプロセスの存在を確認して名前を返す（未設定なら None）。"""
    if bp_id is None:
        return None
    row = conn.execute("SELECT name FROM business_processes WHERE id = ?", (bp_id,)).fetchone()
    if not row:
        raise ValueError("選んだビジネスプロセスは削除されています")
    return row[0]


def _existing_ids(conn, table: str, ids, message: str) -> list[int]:
    ids = list(dict.fromkeys(int(i) for i in ids))
    for i in ids:
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (i,)).fetchone():
            raise ValueError(message)
    return ids


# ---------------------------------------------------------------- 書き込み

def _next_code(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COALESCE(MAX(CAST(SUBSTR(code, 3) AS INTEGER)), 0) + 1 FROM metrics").fetchone()[0]
    return f"M-{n:03d}"


def create_metric(conn: sqlite3.Connection, actor: User, name: str, asset_ids=(), question_ids=(), **optional) -> int:
    """指標を登録する。必須は name だけ。"""
    require(actor, Perm.EDIT)
    bad = set(optional) - set(FIELD_LABELS)
    if bad:
        raise ValueError(f"未対応の項目です: {', '.join(sorted(bad))}")
    values = {f: _normalize(f, optional.get(f)) for f in FIELD_LABELS if f != "name"}
    values["name"] = _normalize("name", name)
    with write_tx(conn):
        bp_name = _check_bp(conn, values["business_process_id"])
        a_ids = _existing_ids(conn, "assets", asset_ids, "紐づけようとしたアセットが削除されています")
        q_ids = _existing_ids(conn, "questions", question_ids, "紐づけようとした問いが削除されています")
        code = _next_code(conn)
        cols = list(values)
        cur = conn.execute(
            f"INSERT INTO metrics (code, created_by, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))})",
            (code, actor.id, *[values[c] for c in cols]),
        )
        mid = cur.lastrowid
        changelog.log(conn, actor, "metric", "create", target=code, new=values["name"])
        if bp_name:
            changelog.log(conn, actor, "metric", "update", target=f"{code} / {FIELD_LABELS['business_process_id']}",
                          new=bp_name)
        for aid in a_ids:
            conn.execute("INSERT INTO metric_assets (metric_id, asset_id) VALUES (?, ?)", (mid, aid))
            changelog.log(conn, actor, "metric", "add", asset_id=aid, target=code, new=values["name"])
        for qid in q_ids:
            conn.execute("INSERT INTO metric_questions (metric_id, question_id) VALUES (?, ?)", (mid, qid))
            changelog.log(conn, actor, "metric", "add", target=code, new=f"問い {_question_code(conn, qid)}")
    return mid


def update_metric(conn: sqlite3.Connection, actor: User, metric_id: int, **changes) -> None:
    """渡された項目だけを変更する（項目単位のあと勝ち）。ビジネスプロセスを外すときは 0 を渡す。"""
    require(actor, Perm.EDIT)
    bad = set(changes) - set(FIELD_LABELS)
    if bad:
        raise ValueError(f"変更できない項目です: {', '.join(sorted(bad))}")
    new = {f: _normalize(f, v) for f, v in changes.items()}
    with write_tx(conn):
        cur = get_metric(conn, metric_id)
        if cur is None:
            raise ValueError("この指標は削除されています")
        if "business_process_id" in new:
            _check_bp(conn, new["business_process_id"])
        diffs = {f: v for f, v in new.items() if v != cur[f]}
        if not diffs:
            return
        conn.execute(
            f"UPDATE metrics SET {', '.join(f'{f} = ?' for f in diffs)}, "
            "updated_at = datetime('now', 'localtime') WHERE id = ?",
            (*diffs.values(), metric_id),
        )
        for f, v in diffs.items():
            old_v = cur[f]
            if f == "business_process_id":  # 履歴には ID ではなく名前を残す
                old_v, v = cur["business_process"], _check_bp(conn, v)
            changelog.log(conn, actor, "metric", "update", target=f"{cur['code']} / {FIELD_LABELS[f]}",
                          old=old_v, new=v)


def link_assets(conn: sqlite3.Connection, actor: User, metric_id: int, add=(), remove=()) -> None:
    """指標と元アセットの紐づけを差分で変える（他の人が同時に足した紐づけは消さない）。"""
    require(actor, Perm.EDIT)
    with write_tx(conn):
        m = get_metric(conn, metric_id)
        if m is None:
            raise ValueError("この指標は削除されています")
        for aid in remove:
            cur = conn.execute("DELETE FROM metric_assets WHERE metric_id = ? AND asset_id = ?", (metric_id, aid))
            if cur.rowcount:
                changelog.log(conn, actor, "metric", "remove", asset_id=aid, target=m["code"], old=m["name"])
        for aid in _existing_ids(conn, "assets", add, "紐づけようとしたアセットが削除されています"):
            cur = conn.execute(
                "INSERT OR IGNORE INTO metric_assets (metric_id, asset_id) VALUES (?, ?)", (metric_id, aid)
            )
            if cur.rowcount:
                changelog.log(conn, actor, "metric", "add", asset_id=aid, target=m["code"], new=m["name"])


def link_questions(conn: sqlite3.Connection, actor: User, metric_id: int, add=(), remove=()) -> None:
    """指標と問いの紐づけを差分で変える。"""
    require(actor, Perm.EDIT)
    with write_tx(conn):
        m = get_metric(conn, metric_id)
        if m is None:
            raise ValueError("この指標は削除されています")
        for qid in remove:
            code = _question_code(conn, qid)
            cur = conn.execute("DELETE FROM metric_questions WHERE metric_id = ? AND question_id = ?",
                               (metric_id, qid))
            if cur.rowcount:
                changelog.log(conn, actor, "metric", "remove", target=m["code"], old=f"問い {code}")
        for qid in _existing_ids(conn, "questions", add, "紐づけようとした問いが削除されています"):
            cur = conn.execute(
                "INSERT OR IGNORE INTO metric_questions (metric_id, question_id) VALUES (?, ?)", (metric_id, qid)
            )
            if cur.rowcount:
                changelog.log(conn, actor, "metric", "add", target=m["code"], new=f"問い {_question_code(conn, qid)}")


def _question_code(conn, question_id: int) -> str:
    row = conn.execute("SELECT code FROM questions WHERE id = ?", (question_id,)).fetchone()
    return row[0] if row else f"#{question_id}"


# ---------------------------------------------------------------- 参照

_SELECT = (
    "SELECT m.*, b.name AS business_process FROM metrics m "
    "LEFT JOIN business_processes b ON b.id = m.business_process_id"
)


def _attach(conn, m: dict) -> dict:
    m["assets"] = [
        dict(r)
        for r in conn.execute(
            "SELECT a.id, a.name, a.status FROM metric_assets ma JOIN assets a ON a.id = ma.asset_id "
            "WHERE ma.metric_id = ? ORDER BY a.name",
            (m["id"],),
        )
    ]
    m["questions"] = [
        dict(r)
        for r in conn.execute(
            "SELECT q.id, q.code, q.text, q.status FROM metric_questions mq JOIN questions q ON q.id = mq.question_id "
            "WHERE mq.metric_id = ? ORDER BY q.code",
            (m["id"],),
        )
    ]
    return m


def get_metric(conn: sqlite3.Connection, metric_id: int) -> dict | None:
    r = conn.execute(f"{_SELECT} WHERE m.id = ?", (metric_id,)).fetchone()
    return _attach(conn, dict(r)) if r else None


def list_metrics(
    conn: sqlite3.Connection,
    query: str = "",
    business_process_id: int | None = None,
    asset_id: int | None = None,
    question_id: int | None = None,
    no_assets: bool = False,
) -> list[dict]:
    sql = f"{_SELECT} WHERE 1 = 1"
    params: list = []
    if query.strip():
        like = f"%{query.strip()}%"
        sql += (
            " AND (m.name LIKE ? OR m.code LIKE ? OR m.description LIKE ? OR m.formula LIKE ? OR m.grain LIKE ? "
            "OR EXISTS (SELECT 1 FROM metric_assets ma JOIN assets a ON a.id = ma.asset_id "
            "WHERE ma.metric_id = m.id AND a.name LIKE ?))"
        )
        params += [like] * 6
    if business_process_id is not None:
        sql += " AND m.business_process_id = ?"
        params.append(business_process_id)
    if asset_id is not None:
        sql += " AND EXISTS (SELECT 1 FROM metric_assets ma WHERE ma.metric_id = m.id AND ma.asset_id = ?)"
        params.append(asset_id)
    if question_id is not None:
        sql += " AND EXISTS (SELECT 1 FROM metric_questions mq WHERE mq.metric_id = m.id AND mq.question_id = ?)"
        params.append(question_id)
    if no_assets:
        sql += " AND NOT EXISTS (SELECT 1 FROM metric_assets ma WHERE ma.metric_id = m.id)"
    sql += " ORDER BY m.id DESC"
    return [_attach(conn, dict(r)) for r in conn.execute(sql, params).fetchall()]


def metrics_for_asset(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    return list_metrics(conn, asset_id=asset_id)


def metrics_for_question(conn: sqlite3.Connection, question_id: int) -> list[dict]:
    return list_metrics(conn, question_id=question_id)


def suggested_values(conn: sqlite3.Connection, field: str) -> list[str]:
    """単位・集計粒度など、自由入力欄のサジェスト（登録済みの値を使用頻度順に）。"""
    if field not in ("unit", "grain"):
        raise ValueError("サジェストできない項目です")
    return [
        r[0]
        for r in conn.execute(
            f"SELECT {field} FROM metrics WHERE {field} <> '' GROUP BY {field} ORDER BY COUNT(*) DESC, {field}"
        )
    ]


# ---------------------------------------------------------------- やわらかい支援

def hints(m: dict) -> list[str]:
    """指標を育てるための「次に書くとよいこと」。止めるためではなく、気づくためのもの。"""
    out = []
    if not m["business_process_id"]:
        out.append("ビジネスプロセスを選ぶと、どの業務から生まれる数字かが分かります")
    if not m["description"]:
        out.append("定義（何を表す数字か）を書いておきましょう")
    if not m["formula"]:
        out.append("計算式を文章で書いておくと、部門ごとに数字がずれるのを防げます")
    if not m["grain"]:
        out.append("集計粒度（例: 日次×店舗、注文明細1行）を書いておくと、他の指標と比べやすくなります")
    if not m["assets"]:
        out.append("元になるデータを紐づけると、オーナーとスチュワードが分かります")
    if unusable := [f"{a['name']}（{a['status']}）" for a in m["assets"] if a["status"] != ACTIVE]:
        out.append(f"運用中でないデータを使っています: {'、'.join(unusable)}。代わりのデータを検討しましょう")
    return out
