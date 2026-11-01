"""ユーザー管理（管理者のみ）。ユーザーは削除せず無効化する（変更履歴を残すため）。"""

import secrets

import streamlit as st

from catalog import auth
from catalog.auth import ROLES, Perm
from catalog.ui import require_page

conn, user = require_page(Perm.ADMIN)

st.title("👥 ユーザー管理")
st.caption(
    "ロール: 閲覧者 ＜ 編集者（説明・タグ・プロパティ・カラム・リネージの編集）＜ "
    "パワーユーザー（アセットの登録・データ更新・削除）＜ 管理者（プロパティ定義・ユーザー管理・全体の変更履歴）"
)
if msg := st.session_state.pop("users_flash", None):
    st.success(msg)

ROLE_KEYS = list(ROLES)


def flash(message: str) -> None:
    st.session_state["users_flash"] = message
    st.rerun()


# ---- 一覧
users = auth.list_users(conn)
st.dataframe(
    [
        {
            "ユーザー ID": u["username"],
            "表示名": u["display_name"],
            "メールアドレス": u["email"],
            "説明": u["description"],
            "ロール": ROLES[u["role"]],
            "状態": "有効" if u["active"] else "無効",
            "パスワード変更待ち": "はい" if u["must_change_password"] else "",
            "最終ログイン": u["last_login_at"] or "",
            "作成日時": u["created_at"],
        }
        for u in users
    ],
    hide_index=True,
    width="stretch",
)

# ---- 追加
st.subheader("ユーザーを追加")
with st.form("add_user"):
    c1, c2, c3 = st.columns(3)
    username = c1.text_input("ユーザー ID")
    display = c2.text_input("表示名")
    c4, c5 = st.columns([1, 2])
    email = c4.text_input("メールアドレス（任意）", placeholder="name@example.com")
    description = c5.text_input("説明（任意）", placeholder="所属・担当など。例: 営業管理課。受注データの窓口")
    role = c3.selectbox("ロール", ROLE_KEYS, index=ROLE_KEYS.index("editor"), format_func=ROLES.get)
    suggested = st.session_state.setdefault("suggested_pw", secrets.token_urlsafe(9))
    pw = st.text_input("仮パスワード（初回ログイン時に変更を求めます）", value=suggested)
    if st.form_submit_button("追加", type="primary"):
        try:
            auth.create_user(conn, user, username, display, role, pw, email=email, description=description)
        except (ValueError, PermissionError) as e:
            st.error(str(e))
        else:
            st.session_state.pop("suggested_pw", None)
            flash(f"{username.strip()} を追加しました。仮パスワード「{pw}」を本人に伝えてください。")

# ---- 編集
st.subheader("編集")
for u in users:
    label = f"{u['username']}（{u['display_name']} / {ROLES[u['role']]}{' / 無効' if not u['active'] else ''}）"
    with st.expander(label):
        with st.form(f"edit_user_{u['id']}"):
            c1, c2, c3 = st.columns(3)
            new_display = c1.text_input("表示名", value=u["display_name"])
            new_role = c2.selectbox("ロール", ROLE_KEYS, index=ROLE_KEYS.index(u["role"]), format_func=ROLES.get)
            new_active = c3.checkbox("有効", value=bool(u["active"]))
            c4, c5 = st.columns([1, 2])
            new_email = c4.text_input("メールアドレス", value=u["email"])
            new_description = c5.text_input("説明", value=u["description"])
            if st.form_submit_button("保存"):
                try:
                    auth.update_user(conn, user, u["id"], role=new_role, display_name=new_display, active=new_active,
                                     email=new_email, description=new_description)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    flash(f"{u['username']} を更新しました")
        with st.form(f"reset_pw_{u['id']}"):
            new_pw = st.text_input("新しい仮パスワード", value=secrets.token_urlsafe(9), key=f"reset_value_{u['id']}")
            if st.form_submit_button("パスワードをリセット"):
                try:
                    auth.reset_password(conn, user, u["id"], new_pw)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    flash(f"{u['username']} のパスワードをリセットしました。仮パスワード「{new_pw}」を本人に伝えてください。")
