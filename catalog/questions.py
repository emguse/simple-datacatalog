"""問い（問い台帳）: データが答えるべき問い。

docs/question-driven.md の「データは問いに答えるために存在する」を、やわらかく取り入れる。
- 登録に必須なのは問いの文だけ。ほかの項目は後から育てる
- 足りないところはエラーにせず hints() で「次に書くとよいこと」として示す
- 使うデータ・受諾者・監視担当は、既存の情報から候補を出す
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date, timedelta

from catalog import changelog, lineage, roles
from catalog.asset_status import ACTIVE, RETIRED
from catalog.auth import Perm, User, require
from catalog.tx import write_tx

KINDS = ["", "分析", "業務"]
DECISIONS = ["申請中", "受諾", "条件付き受諾", "却下"]
ACCEPTED = ("受諾", "条件付き受諾")
STATUSES = ["有効", "廃止"]
REVIEW_INTERVAL_DAYS = 183  # 見直し日の候補（半年後）

FIELD_LABELS = {
    "text": "問い",
    "kind": "種別",
    "requester_dept": "持ち込んだ部門",
    "requester": "依頼者",
    "required_quality": "必要な品質",
    "decision": "受諾状態",
    "decided_by": "受諾者",
    "decided_at": "受諾日",
    "condition_note": "受諾の条件",
    "monitor": "監視担当",
    "quality_metric": "品質指標",
    "review_date": "見直し日",
    "status": "状態",
}
DATE_FIELDS = ("decided_at", "review_date")

# docs §6.2 の例。入力欄の placeholder や空のときの例示に使う
EXAMPLES = [
    "得意先別の月次粗利はいくらか？",
    "今月の受注見込はどれくらいか？",
    "この注文は出荷済みか？",
    "月次決算の売上高はいくらか？",
]


# ---------------------------------------------------------------- 検証

def _normalize(field: str, value) -> str:
    if isinstance(value, date):
        value = value.isoformat()
    v = "" if value is None else str(value).strip()
    if field == "text" and not v:
        raise ValueError("問いを入力してください")
    if field == "kind" and v not in KINDS:
        raise ValueError(f"種別は {' / '.join(k for k in KINDS if k)} のどれかです")
    if field == "decision" and v not in DECISIONS:
        raise ValueError(f"受諾状態は {' / '.join(DECISIONS)} のどれかです")
    if field == "status" and v not in STATUSES:
        raise ValueError(f"状態は {' / '.join(STATUSES)} のどちらかです")
    if field in DATE_FIELDS and v:
        try:
            v = date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError(f"{FIELD_LABELS[field]}は YYYY-MM-DD 形式で入力してください") from None
    return v


# ---------------------------------------------------------------- 書き込み

def _next_code(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COALESCE(MAX(CAST(SUBSTR(code, 3) AS INTEGER)), 0) + 1 FROM questions").fetchone()[0]
    return f"Q-{n:03d}"


def _asset_ids_exist(conn, ids) -> list[int]:
    ids = list(dict.fromkeys(int(i) for i in ids))
    for i in ids:
        if not conn.execute("SELECT 1 FROM assets WHERE id = ?", (i,)).fetchone():
            raise ValueError("紐づけようとしたアセットが削除されています")
    return ids


def create_question(conn: sqlite3.Connection, actor: User, text: str, asset_ids=(), **optional) -> int:
    """問いを登録する。必須は text だけ。依頼者が空なら登録した人の表示名を入れる。"""
    require(actor, Perm.EDIT)
    bad = set(optional) - set(FIELD_LABELS)
    if bad:
        raise ValueError(f"未対応の項目です: {', '.join(sorted(bad))}")
    defaults = {"decision": "申請中", "status": "有効"}
    values = {f: _normalize(f, optional.get(f) or defaults.get(f, "")) for f in FIELD_LABELS if f != "text"}
    values["text"] = _normalize("text", text)
    values["requester"] = values["requester"] or actor.display_name
    if values["decision"] in ACCEPTED and not values["decided_at"]:
        values["decided_at"] = date.today().isoformat()
    with write_tx(conn):
        ids = _asset_ids_exist(conn, asset_ids)
        code = _next_code(conn)
        cols = list(values)
        cur = conn.execute(
            f"INSERT INTO questions (code, created_by, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))})",
            (code, actor.id, *[values[c] for c in cols]),
        )
        qid = cur.lastrowid
        changelog.log(conn, actor, "question", "create", target=code, new=values["text"])
        for aid in ids:
            conn.execute("INSERT INTO question_assets (question_id, asset_id) VALUES (?, ?)", (qid, aid))
            changelog.log(conn, actor, "question", "add", asset_id=aid, target=code, new=values["text"])
    return qid


def update_question(conn: sqlite3.Connection, actor: User, question_id: int, **changes) -> None:
    """渡された項目だけを変更する（項目単位のあと勝ち）。

    受諾状態を受諾／条件付き受諾にして受諾日が空なら、今日を入れる。
    """
    require(actor, Perm.EDIT)
    bad = set(changes) - set(FIELD_LABELS)
    if bad:
        raise ValueError(f"変更できない項目です: {', '.join(sorted(bad))}")
    new = {f: _normalize(f, v) for f, v in changes.items()}
    with write_tx(conn):
        cur = get_question(conn, question_id)
        if cur is None:
            raise ValueError("この問いは削除されています")
        if new.get("decision") in ACCEPTED and not (new.get("decided_at") or cur["decided_at"]):
            new["decided_at"] = date.today().isoformat()
        diffs = {f: v for f, v in new.items() if v != cur[f]}
        if not diffs:
            return
        conn.execute(
            f"UPDATE questions SET {', '.join(f'{f} = ?' for f in diffs)}, "
            "updated_at = datetime('now', 'localtime') WHERE id = ?",
            (*diffs.values(), question_id),
        )
        for f, v in diffs.items():
            changelog.log(conn, actor, "question", "update", target=f"{cur['code']} / {FIELD_LABELS[f]}",
                          old=cur[f], new=v)


def link_assets(conn: sqlite3.Connection, actor: User, question_id: int, add=(), remove=()) -> None:
    """問いとアセットの紐づけを差分で変える（他の人が同時に足した紐づけは消さない）。"""
    require(actor, Perm.EDIT)
    with write_tx(conn):
        q = get_question(conn, question_id)
        if q is None:
            raise ValueError("この問いは削除されています")
        for aid in remove:
            cur = conn.execute(
                "DELETE FROM question_assets WHERE question_id = ? AND asset_id = ?", (question_id, aid)
            )
            if cur.rowcount:
                changelog.log(conn, actor, "question", "remove", asset_id=aid, target=q["code"], old=q["text"])
        for aid in _asset_ids_exist(conn, add):
            cur = conn.execute(
                "INSERT OR IGNORE INTO question_assets (question_id, asset_id) VALUES (?, ?)", (question_id, aid)
            )
            if cur.rowcount:
                changelog.log(conn, actor, "question", "add", asset_id=aid, target=q["code"], new=q["text"])


# ---------------------------------------------------------------- 参照

def _attach_assets(conn, q: dict) -> dict:
    q["assets"] = [
        dict(r)
        for r in conn.execute(
            "SELECT a.id, a.name, a.status FROM question_assets qa JOIN assets a ON a.id = qa.asset_id "
            "WHERE qa.question_id = ? ORDER BY a.name",
            (q["id"],),
        )
    ]
    return q


def get_question(conn: sqlite3.Connection, question_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    return _attach_assets(conn, dict(r)) if r else None


def is_review_due(q: dict, today: date | None = None) -> bool:
    if q["status"] != "有効" or not q["review_date"]:
        return False
    return date.fromisoformat(q["review_date"]) <= (today or date.today())


def list_questions(
    conn: sqlite3.Connection,
    query: str = "",
    status: str | None = None,
    decision: str | None = None,
    kind: str | None = None,
    asset_id: int | None = None,
    review_due: bool = False,
    no_assets: bool = False,
    today: date | None = None,
) -> list[dict]:
    sql = "SELECT q.* FROM questions q WHERE 1 = 1"
    params: list = []
    if query.strip():
        like = f"%{query.strip()}%"
        sql += (
            " AND (q.text LIKE ? OR q.code LIKE ? OR q.required_quality LIKE ? OR q.requester_dept LIKE ? "
            "OR EXISTS (SELECT 1 FROM question_assets qa JOIN assets a ON a.id = qa.asset_id "
            "WHERE qa.question_id = q.id AND a.name LIKE ?))"
        )
        params += [like] * 5
    for col, val in (("status", status), ("decision", decision), ("kind", kind)):
        if val is not None:
            sql += f" AND q.{col} = ?"
            params.append(val)
    if asset_id is not None:
        sql += " AND EXISTS (SELECT 1 FROM question_assets qa WHERE qa.question_id = q.id AND qa.asset_id = ?)"
        params.append(asset_id)
    if no_assets:
        sql += " AND NOT EXISTS (SELECT 1 FROM question_assets qa WHERE qa.question_id = q.id)"
    sql += " ORDER BY q.status = '廃止', q.id DESC"
    out = [_attach_assets(conn, dict(r)) for r in conn.execute(sql, params).fetchall()]
    if review_due:
        out = [q for q in out if is_review_due(q, today)]
    return out


def questions_for_asset(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    return list_questions(conn, asset_id=asset_id)


def downstream_questions(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    """リネージの下流にあるアセットで使われている（有効な）問い。

    上流のデータを守る人に「このデータがどの問いを支えているか」を見せるためのもの。
    このアセットに直接紐づいた問いは除く。各問いに via（経由する下流アセット名）を付ける。
    """
    down = lineage.reachable(conn, asset_id, "down")
    if not down:
        return []
    direct = {q["id"] for q in questions_for_asset(conn, asset_id)}
    found: dict[int, dict] = {}
    for d in sorted(down):
        for q in list_questions(conn, asset_id=d, status="有効"):
            if q["id"] in direct:
                continue
            entry = found.setdefault(q["id"], {**q, "via": []})
            name = next(a["name"] for a in q["assets"] if a["id"] == d)
            entry["via"].append(name)
    return sorted(found.values(), key=lambda q: q["code"])


def active_question_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT qa.asset_id, COUNT(*) FROM question_assets qa JOIN questions q ON q.id = qa.question_id "
            "WHERE q.status = '有効' GROUP BY qa.asset_id"
        )
    }


# ---------------------------------------------------------------- やわらかい支援

def looks_like_question(text: str) -> bool:
    t = text.strip().rstrip("。．. 　")
    return bool(re.search(r"([?？]|か|かな|のか|ですか|ますか)$", t))


def hints(q: dict, today: date | None = None) -> list[str]:
    """問いを育てるための「次に書くとよいこと」。止めるためではなく、気づくためのもの。"""
    if q["status"] == "廃止":
        return []
    out = []
    if not looks_like_question(q["text"]):
        out.append("疑問文（〜か？）にすると、何を知りたいかが伝わりやすくなります")
    if not q.get("assets"):
        out.append("答えに使うデータを紐づけると、オーナーとスチュワードが分かります")
    if unusable := [f"{a['name']}（{a['status']}）" for a in q.get("assets", []) if a["status"] != ACTIVE]:
        out.append(f"運用中でないデータを使っています: {'、'.join(unusable)}。代わりのデータを検討しましょう")
    if not q["required_quality"]:
        out.append("「必要な品質」（鮮度・粒度・正確性・網羅性など）を書いておくと、受諾の判断がしやすくなります")
    if q["decision"] in ACCEPTED:
        if not q["decided_by"]:
            out.append("受諾を判断した人（受諾者）を記録しておきましょう")
        if not q["monitor"]:
            out.append("受諾済みです。品質を見守る監視担当（スチュワード）を決めておきましょう")
        if q["decision"] == "条件付き受諾" and not q["condition_note"]:
            out.append("条件付き受諾の「条件」を書いておくと、あとで見直しやすくなります")
        if not q["review_date"]:
            out.append("見直し日を決めておくと、使われなくなった問いが残り続けるのを防げます")
    if is_review_due(q, today):
        out.append("見直し日を過ぎています。この問いはまだ有効ですか？ 必要な品質は変わっていませんか？")
    return out


def default_review_date(today: date | None = None) -> str:
    return ((today or date.today()) + timedelta(days=REVIEW_INTERVAL_DAYS)).isoformat()


_HIRAGANA = re.compile(r"^[぀-ゟ]+$")
_DROP = re.compile(r"[\s\W_]+", re.UNICODE)


def _bigrams(text: str) -> set[str]:
    """文字 bigram（形態素解析なしで日本語の近さを測るため）。助詞だけの組（ひらがなのみ）は捨てる。"""
    t = _DROP.sub(" ", unicodedata.normalize("NFKC", text).lower())
    grams = set()
    for chunk in t.split():
        if len(chunk) == 1:
            if not _HIRAGANA.match(chunk):
                grams.add(chunk)
            continue
        for i in range(len(chunk) - 1):
            g = chunk[i : i + 2]
            if not _HIRAGANA.match(g):
                grams.add(g)
    return grams


def _asset_document(conn, asset_id: int) -> str:
    a = conn.execute(
        "SELECT a.name, a.description, COALESCE(d.name, '') FROM assets a LEFT JOIN domains d ON d.id = a.domain_id "
        "WHERE a.id = ?",
        (asset_id,),
    ).fetchone()
    parts = [a[0].replace("_", " "), a[1], a[2]]
    parts += [f"{r[0].replace('_', ' ')} {r[1]}" for r in conn.execute(
        "SELECT name, description FROM columns WHERE asset_id = ?", (asset_id,))]
    parts += [r[0] for r in conn.execute(
        "SELECT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id WHERE at.asset_id = ?", (asset_id,))]
    parts += [r[0] for r in conn.execute("SELECT value FROM asset_properties WHERE asset_id = ?", (asset_id,))]
    return " ".join(parts)


def suggest_assets(conn: sqlite3.Connection, text: str, limit: int = 5) -> list[dict]:
    """問いの文に近いアセットの候補（名前・説明・カラム・タグ・プロパティとの文字 bigram の重なり）。

    すでに同じアセットを使っている問いの文も手がかりにする。廃止したアセットは候補にしない。
    """
    q = _bigrams(text)
    if not q:
        return []
    scored = []
    for r in conn.execute("SELECT id, name, status FROM assets WHERE status <> ? ORDER BY name", (RETIRED,)).fetchall():
        doc = _asset_document(conn, r["id"])
        doc += " " + " ".join(x["text"] for x in questions_for_asset(conn, r["id"]))
        hit = q & _bigrams(doc)
        if hit:
            scored.append({"id": r["id"], "name": r["name"], "status": r["status"], "score": len(hit) / len(q),
                           "matched": sorted(hit)})
    scored.sort(key=lambda x: (-x["score"], x["name"]))
    return scored[:limit]


def similar_questions(conn: sqlite3.Connection, text: str, threshold: float = 0.4, limit: int = 5) -> list[dict]:
    """登録済みの似た問い（重複登録を避けるため）。"""
    q = _bigrams(text)
    if not q:
        return []
    out = []
    for row in list_questions(conn):
        other = _bigrams(row["text"])
        if not other:
            continue
        score = len(q & other) / len(q | other)
        if score >= threshold:
            out.append({**row, "score": score})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def suggested_deciders(conn: sqlite3.Connection, asset_ids) -> list[str]:
    """受諾者の候補: 紐づくアセットに効いているオーナーのうち「問いの受諾」を受け持つ人（役職）。"""
    out: list[str] = []
    for aid in asset_ids:
        for a in roles.effective_assignments(conn, aid):
            if a["kind"] == "owner" and "問いの受諾" in a["scopes"]:
                label = f"{a['org']} {a['position']}"
                if label not in out:
                    out.append(label)
    return out


def suggested_monitors(conn: sqlite3.Connection, asset_ids) -> list[str]:
    """監視担当の候補: 紐づくアセットに効いているスチュワード（人）。"""
    out: list[str] = []
    for aid in asset_ids:
        for a in roles.effective_assignments(conn, aid):
            if a["kind"] == "steward":
                label = f"{a['org']} {a['person']}"
                if label not in out:
                    out.append(label)
    return out
