"""Streamlit 画面で共通に使うヘルパー。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import streamlit as st

from catalog import auth
from catalog import roles as _roles
from catalog import uploads as _uploads
from catalog import repository as repo
from catalog.asset_status import ACTIVE, DEPRECATED
from catalog.auth import Perm, User
from catalog.db import DEFAULT_DB_PATH, connect, init_db
from catalog.ingest import FileSchema

DETAIL_PAGE = "pages/2_アセット詳細.py"
LINEAGE_PAGE = "pages/3_リネージ.py"

UPLOAD_DIR = DEFAULT_DB_PATH.parent / "uploads"
STAGING_DIR = UPLOAD_DIR / ".staging"

_DROP = "(削除する)"


@st.cache_resource
def _init_once(path: str) -> bool:
    init_db(path)
    return True


def get_conn() -> sqlite3.Connection:
    """呼ぶたびに新しい接続を返す。

    接続をセッション・スレッド間で共有すると、同時に書き込んだときにトランザクションが混ざる。
    SQLite の接続は軽いので、ページの実行ごとに作り、参照がなくなれば閉じられる。
    """
    _init_once(str(DEFAULT_DB_PATH))
    return connect()


# ---------------------------------------------------------------- ログイン・権限

def current_user(conn: sqlite3.Connection) -> User | None:
    """ログイン中のユーザー。毎回 DB から読み直すので、無効化やロール変更はすぐ反映される。"""
    uid = st.session_state.get("user_id")
    if uid is None:
        return None
    user = auth.get_user(conn, uid)
    if user is None or not user.active:
        st.session_state.pop("user_id", None)
        return None
    return user


def require_page(perm: Perm) -> tuple[sqlite3.Connection, User]:
    """ページ先頭で呼ぶ。権限がなければメッセージを出して止める（URL 直打ち対策）。"""
    conn = get_conn()
    user = current_user(conn)
    if not auth.can(user, perm):
        st.error(f"このページを開くには「{auth.ROLES[auth.PERM_ROLE[perm]]}」以上の権限が必要です。")
        st.stop()
    return conn, user


def logout() -> None:
    for k in list(st.session_state):
        del st.session_state[k]


# ---------------------------------------------------------------- 差分保存用のスナップショット

class FormSnapshot:
    """フォームを開いた時点の値を覚えておき、保存時に「変えた項目」だけを取り出すための入れ物。

    - values: 最初に描いたときに loader() で読んだ値（保存・再読み込みまで固定）
    - wkey(): ウィジェットのキー。reset() で世代が変わり、ウィジェットも作り直される
    - stale: 開いた後に他のユーザーが DB を変えたか
    """

    def __init__(self, name: str, asset_id: int, loader):
        self._base = f"snap_{name}_{asset_id}"
        self._loader = loader
        gen_key = f"{self._base}_gen"
        self.gen = st.session_state.setdefault(gen_key, 0)
        if f"{self._base}_values" not in st.session_state:
            st.session_state[f"{self._base}_values"] = loader()
        self.values = st.session_state[f"{self._base}_values"]

    def wkey(self, *parts) -> str:
        return "_".join([self._base, str(self.gen), *map(str, parts)])

    @property
    def stale(self) -> bool:
        return self._loader() != self.values

    def reset(self) -> None:
        st.session_state.pop(f"{self._base}_values", None)
        st.session_state[f"{self._base}_gen"] = self.gen + 1

    def stale_notice(self) -> None:
        """他のユーザーが更新していたら知らせ、最新を読み込むボタンを出す。"""
        if self.stale:
            c1, c2 = st.columns([4, 1])
            c1.info("この画面を開いた後に、他のユーザーが内容を更新しました。保存すると、あなたが変えた項目だけが反映されます。")
            if c2.button("最新を読み込む", key=self.wkey("reload")):
                self.reset()
                st.rerun()


def asset_options(conn: sqlite3.Connection) -> dict[int, str]:
    """アセット ID → 表示名。選ぶときに気づけるよう、運用中でないものはステータスを添える。"""
    return {
        r["id"]: r["name"] if r["status"] == ACTIVE else f"{r['name']}（{r['status']}）"
        for r in conn.execute("SELECT id, name, status FROM assets ORDER BY name")
    }


def asset_status_badge(status: str) -> str:
    """運用中でないときのバッジ（運用中は空文字）。"""
    if status == ACTIVE:
        return ""
    return f":{'orange' if status == DEPRECATED else 'gray'}-badge[{status}]"


REGISTER_PAGE = "pages/1_登録.py"


def selected_asset_id(conn: sqlite3.Connection, user: User | None, label: str = "アセット") -> int | None:
    """query param `id` を初期値にしたアセット選択。選び直したら query param も更新する。"""
    options = asset_options(conn)
    if not options:
        st.info("まだアセットが登録されていません。")
        if auth.can(user, Perm.MANAGE_ASSETS):
            st.page_link(REGISTER_PAGE, label="登録ページへ", icon="➕")
        return None
    ids = list(options)
    try:
        current = int(st.query_params.get("id", ids[0]))
    except ValueError:
        current = ids[0]
    if current not in options:
        current = ids[0]
    chosen = st.selectbox(label, ids, index=ids.index(current), format_func=options.get)
    st.query_params["id"] = str(chosen)
    return chosen


def stage_upload(uploaded) -> Path:
    """st.file_uploader で受け取ったファイルを、アップロードごとに一意な staging パスに書き出す。"""
    return _uploads.stage_upload(uploaded, STAGING_DIR)


def store_upload(staged: Path, asset_name: str) -> Path:
    """staging のファイルを uploads/ に移す（ファイル名はアセット名＋一意な接尾辞。古いファイルは消さない）。"""
    return _uploads.store_upload(staged, asset_name, UPLOAD_DIR)


def discard_upload(conn: sqlite3.Connection, path, keep=None) -> bool:
    """uploads/ 直下のファイルで、どのアセットからも参照されていないものだけを削除する。

    DB の更新が成功してから、置き換え前のファイル（または登録に失敗したときに移したファイル）に使う。
    同じファイルをローカルパス指定で登録した別のアセットがあれば、消さずに残す。
    """
    if not path or repo.assets_referencing_file(conn, path):
        return False
    return _uploads.discard_upload(path, UPLOAD_DIR, keep=keep)


def _normalize(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def schema_table(schema: FileSchema) -> None:
    st.dataframe(
        [{"カラム": c.name, "型": c.data_type} for c in schema.columns],
        hide_index=True,
        height=min(35 * (len(schema.columns) + 1) + 3, 300),
    )


def review_changes(conn: sqlite3.Connection, name: str, schema: FileSchema, key: str) -> tuple[dict[str, str], bool]:
    """既存アセットとの差分を表示し、消えたカラムを新カラムに対応付けさせる。

    戻り値は (renames: 旧名 → 新名, 登録してよいか)。
    """
    added, removed = repo.diff_columns(conn, name, schema)
    if repo.get_asset_by_name(conn, name) is None:
        return {}, True

    renames: dict[str, str] = {}
    if removed:
        st.markdown("**見つからなくなったカラム**：名前を変えただけなら、対応する新カラムを選ぶと説明・タグを引き継ぎます。")
        for old in removed:
            options = [_DROP, *added]
            default = next((i for i, n in enumerate(options) if i and _normalize(n) == _normalize(old)), 0)
            choice = st.selectbox(f"`{old}` →", options, index=default, key=f"{key}_rename_{old}")
            if choice != _DROP:
                renames[old] = choice

    targets = list(renames.values())
    if len(set(targets)) != len(targets):
        st.error("同じ新カラムが複数の旧カラムに対応付けられています。")
        return renames, False

    final_added, final_removed = repo.diff_columns(conn, name, schema, renames)
    if renames:
        st.success("引き継ぐカラム: " + ", ".join(f"{o} → {n}" for o, n in renames.items()))
    if final_added:
        st.info(f"追加されるカラム: {', '.join(final_added)}")
    if not (renames or final_added or final_removed):
        st.caption("カラム構成に変更はありません（型・行数のみ更新されます）。")
    if final_removed:
        st.warning(f"削除されるカラム（説明・タグも消えます）: {', '.join(final_removed)}")
        return renames, st.checkbox("削除されるカラムがあることを確認しました", key=f"{key}_confirm")
    return renames, True


# ---------------------------------------------------------------- プロパティ

UNSET = "（未設定）"


def property_label(defn) -> str:
    return f"{defn.name} ＊" if defn.required else defn.name


def property_input(defn, current: str | None, key: str, suggestions: list[str] | None = None):
    """プロパティの型に合った入力ウィジェットを出し、入力値（未設定なら None）を返す。"""
    label = property_label(defn)
    help_ = defn.help or None
    if defn.type == "select":
        options = [UNSET, *defn.choices]
        if current and current not in options:  # 選択肢から外された古い値も表示だけはする
            options.append(current)
        v = st.selectbox(label, options, index=options.index(current) if current else 0, key=key, help=help_)
        return None if v == UNSET else v
    if defn.type == "text":
        # これまでの入力値を候補に出して表記ゆれを防ぐ。新しい値も入力できる
        options = [UNSET, *sorted(set(suggestions or []) | ({current} if current else set()))]
        v = st.selectbox(
            label,
            options,
            index=options.index(current) if current else 0,
            key=key,
            help=(help_ or "") + "（候補から選ぶか、新しい値を入力して Enter）",
            accept_new_options=True,
        )
        return None if v in (None, UNSET) else v
    if defn.type == "longtext":
        return st.text_area(label, value=current or "", key=key, help=help_)
    if defn.type == "date":
        value = None
        if current:
            try:
                value = date.fromisoformat(current)
            except ValueError:
                value = None
        return st.date_input(
            label, value=value, min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=key, help=help_
        )
    # number / url はテキストで受け取り、保存時に検証する
    placeholder = "例: 100" if defn.type == "number" else "https://..."
    return st.text_input(label, value=current or "", key=key, help=help_, placeholder=placeholder)


def completeness_badge(comp) -> str:
    color = "green" if comp.complete else "orange"
    return f":{color}-badge[充実度 {comp.filled}/{comp.total}]"


# ---------------------------------------------------------------- 役割（オーナー／スチュワード）

DOMAIN_PAGE = "pages/7_ドメイン.py"


def _suggest_input(label: str, field: str, current: str, key: str, conn: sqlite3.Connection, help_: str | None = None):
    """これまでの入力値を候補に出す自由入力欄（表記ゆれ防止）。"""
    options = [UNSET, *sorted(set(_roles.suggestions(conn, field)) | ({current} if current else set()))]
    v = st.selectbox(
        label,
        options,
        index=options.index(current) if current else 0,
        key=key,
        help=(help_ + "。" if help_ else "") + "候補から選ぶか、新しい値を入力して Enter",
        accept_new_options=True,
    )
    return "" if v in (None, UNSET) else v


def _assignment_fields(conn, kind: str, values: dict, key) -> dict:
    """種類に応じた入力欄を出し、入力値を返す（フォームの中で呼ぶ）。"""
    req = {f: r for f, r in _roles.KIND_FIELDS[kind]}

    def lbl(f):
        return _roles.FIELD_LABELS[f] + (" ＊" if req[f] else "")

    out: dict = {}
    c1, c2 = st.columns(2)
    with c1:
        out["org"] = _suggest_input(lbl("org"), "org", values.get("org", ""), key("org"), conn, "部門名・課名")
    if kind == "owner":
        with c2:
            out["position"] = _suggest_input(lbl("position"), "position", values.get("position", ""), key("position"),
                                             conn, "異動に強いよう役職で持つ（例: 営業部長）")
        c3, c4 = st.columns(2)
        with c3:
            out["person"] = _suggest_input(lbl("person"), "person", values.get("person", ""), key("person"), conn,
                                           "現在その役職にいる人")
        out["delegation_ref"] = c4.text_input(lbl("delegation_ref"), value=values.get("delegation_ref", ""),
                                              key=key("delegation_ref"), help="ドメインの委譲規程への参照（URL など）")
        out["scopes"] = st.multiselect(lbl("scopes"), _roles.OWNER_SCOPES, default=values.get("scopes", []),
                                       key=key("scopes"))
    else:
        with c2:
            out["person"] = _suggest_input(lbl("person"), "person", values.get("person", ""), key("person"), conn,
                                           "実際に窓口になる人")
        c3, c4 = st.columns(2)
        with c3:
            out["contact"] = _suggest_input(lbl("contact"), "contact", values.get("contact", ""), key("contact"), conn,
                                            "メールアドレス、チャンネルなど")
        with c4:
            out["escalation"] = _suggest_input(lbl("escalation"), "escalation", values.get("escalation", ""),
                                               key("escalation"), conn, "範囲外の問題を上げる先")
        out["scope_note"] = st.text_input(lbl("scope_note"), value=values.get("scope_note", ""), key=key("scope_note"),
                                          help="担当する項目のまとまりや作業")
    lc = values.get("last_confirmed")
    out["last_confirmed"] = st.date_input(
        lbl("last_confirmed"), value=date.fromisoformat(lc) if lc else date.today(), key=key("last_confirmed"),
        help="割り当てが正しいことを最後に確認した日",
    )
    return out


def assignments_table(rows: list[dict], show_target: bool = False, show_source: bool = False) -> None:
    data = []
    for a in rows:
        row = {}
        if show_target:
            row["対象"] = f"ドメイン: {a['domain_name']}" if a.get("domain_id") else f"アセット: {a.get('asset_name')}"
        row.update({
            "役割": a["role_label"],
            "組織": a["org"],
            "役職／担当者": a["position"] + (f"（{a['person']}）" if a["person"] else "") if a["kind"] == "owner" else a["person"],
            "責任・担当の範囲": "・".join(a["scopes"]) if a["kind"] == "owner" else a["scope_note"],
            "窓口": a["contact"],
            "エスカレーション先": a["escalation"],
            "委譲規程": a["delegation_ref"],
        })
        if show_source:
            row["継承元"] = a["source"] + (f"（{a.get('domain_name')}）" if a["source"] == _roles.SOURCE_DOMAIN else "")
        row["最終確認日"] = ("⚠ " if a["stale"] else "") + a["last_confirmed"]
        data.append(row)
    st.dataframe(data, hide_index=True, width="stretch")


def role_editor(conn: sqlite3.Connection, user: User, *, domain_id: int | None = None, asset_id: int | None = None) -> None:
    """対象（ドメインかアセット）に直接付いた割り当ての編集・確認・削除と、新規追加。

    編集は、開いた時点の値と比べて変えた項目だけを保存する（項目単位のあと勝ち）。
    """
    target_key = f"d{domain_id}" if domain_id is not None else f"a{asset_id}"
    own = _roles.assignments_for(conn, domain_id=domain_id, asset_id=asset_id)
    for a in own:
        title = f"{'⚠ ' if _roles.is_stale(a['last_confirmed']) else ''}{a['role_label']}: {_roles.assignment_label(a)}"
        with st.expander(title):
            snap = FormSnapshot("role", a["id"], lambda aid=a["id"]: _roles.get_assignment(conn, aid))
            if snap.values is None:
                st.warning("この割り当ては削除されています。")
                continue
            snap.stale_notice()
            with st.form(snap.wkey("form")):
                inputs = _assignment_fields(conn, a["kind"], snap.values, lambda f: snap.wkey(f))
                if st.form_submit_button("保存", type="primary"):
                    before = snap.values
                    changed = {f: v for f, v in inputs.items()
                               if (v.isoformat() if isinstance(v, date) else v) != before[f]}
                    try:
                        _roles.update_assignment(conn, user, a["id"], **changed)
                    except (ValueError, PermissionError) as e:
                        st.error(str(e))
                    else:
                        snap.reset()
                        st.toast("割り当てを保存しました")
                        st.rerun()
            c1, c2 = st.columns(2)
            if c1.button("確認済みにする（最終確認日を今日に）", key=snap.wkey("confirm")):
                _roles.confirm_assignment(conn, user, a["id"])
                snap.reset()
                st.rerun()
            if c2.checkbox("削除する", key=snap.wkey("del_ck")) and c2.button("削除を実行", key=snap.wkey("del")):
                _roles.delete_assignment(conn, user, a["id"])
                snap.reset()
                st.rerun()

    st.markdown("**割り当てを追加**")
    types = _roles.role_types(conn)
    gen_key = f"role_add_gen_{target_key}"
    gen = st.session_state.setdefault(gen_key, 0)
    rt = st.selectbox("役割の種類", [t["key"] for t in types], format_func={t["key"]: t["label"] for t in types}.get,
                      key=f"role_add_type_{target_key}_{gen}")
    kind = next(t["kind"] for t in types if t["key"] == rt)
    with st.form(f"role_add_{target_key}_{gen}"):
        inputs = _assignment_fields(conn, kind, {}, lambda f: f"role_add_{target_key}_{gen}_{rt}_{f}")
        if st.form_submit_button("追加", type="primary"):
            try:
                _roles.add_assignment(conn, user, role_type=rt, domain_id=domain_id, asset_id=asset_id, **inputs)
            except (ValueError, PermissionError) as e:
                st.error(str(e))
            else:
                st.session_state[gen_key] = gen + 1  # 入力欄を空に戻す
                st.toast("割り当てを追加しました")
                st.rerun()


# ---------------------------------------------------------------- 問い

QUESTIONS_PAGE = "pages/10_問い.py"
_DECISION_COLORS = {"申請中": "gray", "受諾": "green", "条件付き受諾": "orange", "却下": "red"}


def question_badges(q: dict) -> str:
    badges = [f":{_DECISION_COLORS[q['decision']]}-badge[{q['decision']}]"]
    if q["kind"]:
        badges.append(f":blue-badge[{q['kind']}]")
    if q["status"] == "廃止":
        badges.append(":gray-badge[廃止]")
    return " ".join(badges)


def question_line(q: dict, link: bool = True) -> None:
    """問いを1行で表示する（番号・文・バッジ）。link=True なら問いページへのリンクを付ける。"""
    c1, c2 = st.columns([5, 1])
    c1.markdown(f"**{q['code']}** {q['text']}　{question_badges(q)}")
    if link:
        c2.page_link(QUESTIONS_PAGE, label="開く", icon="❓", query_params={"q": q["id"]})


# ---------------------------------------------------------------- 指標

METRICS_PAGE = "pages/11_指標.py"


def metric_line(m: dict, link: bool = True) -> None:
    """指標を1行で表示する（番号・名前・ビジネスプロセス・単位）。link=True なら指標ページへのリンクを付ける。"""
    c1, c2 = st.columns([5, 1])
    badges = [f":blue-badge[{m['business_process']}]"] if m.get("business_process") else []
    if m["unit"]:
        badges.append(f":gray-badge[{m['unit']}]")
    c1.markdown(f"**{m['code']}** {m['name']}　{' '.join(badges)}")
    if link:
        c2.page_link(METRICS_PAGE, label="開く", icon="📈", query_params={"m": m["id"]})
