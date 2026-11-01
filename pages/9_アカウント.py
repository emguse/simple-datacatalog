"""自分のアカウント（プロフィールとパスワード変更）。初回ログインやリセット後はこのページだけが表示される。"""

import streamlit as st

from catalog import auth
from catalog.auth import Perm
from catalog.ui import require_page

conn, user = require_page(Perm.VIEW)

st.title("👤 アカウント")
if msg := st.session_state.pop("account_flash", None):
    st.success(msg)
if user.must_change_password:
    st.warning("管理者が設定した仮パスワードでログインしています。新しいパスワードに変更してください。")

st.markdown(f"**{user.display_name}**（{user.username}）・ ロール: {user.role_label}")

st.subheader("プロフィール")
with st.form("profile"):
    email = st.text_input("メールアドレス", value=user.email, placeholder="name@example.com")
    description = st.text_area("説明", value=user.description, placeholder="所属・担当など。例: 営業管理課。受注データの窓口")
    if st.form_submit_button("保存"):
        try:
            auth.update_own_profile(
                conn, user,
                email=email if email.strip().lower() != user.email else None,
                description=description if description.strip() != user.description else None,
            )
        except (ValueError, PermissionError) as e:
            st.error(str(e))
        else:
            st.session_state["account_flash"] = "プロフィールを保存しました"
            st.rerun()

st.subheader("パスワード変更")
with st.form("change_password", clear_on_submit=True):
    old = st.text_input("現在のパスワード", type="password")
    new = st.text_input("新しいパスワード", type="password", help=f"{auth.MIN_PASSWORD_LENGTH} 文字以上")
    new2 = st.text_input("新しいパスワード（確認）", type="password")
    if st.form_submit_button("変更する", type="primary"):
        if new != new2:
            st.error("新しいパスワードが一致しません")
        else:
            try:
                auth.change_own_password(conn, user, old, new)
            except ValueError as e:
                st.error(str(e))
            else:
                st.session_state["account_flash"] = "パスワードを変更しました"
                st.rerun()
