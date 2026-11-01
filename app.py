"""エントリポイント（ルーター）: ログインとロールに応じたページの振り分け。"""

import time

import streamlit as st

from catalog import auth
from catalog.auth import Perm
from catalog.ui import current_user, get_conn, logout

st.set_page_config(page_title="Simple Data Catalog", page_icon="📚", layout="wide")


def login_page() -> None:
    conn = get_conn()
    st.title("📚 Simple Data Catalog")
    if not auth.has_users(conn):
        st.subheader("初期セットアップ")
        st.write("ユーザーがまだいません。最初の管理者アカウントを作成してください。")
        with st.form("bootstrap"):
            username = st.text_input("ユーザー ID")
            display = st.text_input("表示名")
            email = st.text_input("メールアドレス（任意）")
            pw = st.text_input("パスワード", type="password", help=f"{auth.MIN_PASSWORD_LENGTH} 文字以上")
            pw2 = st.text_input("パスワード（確認）", type="password")
            if st.form_submit_button("管理者を作成", type="primary"):
                if pw != pw2:
                    st.error("パスワードが一致しません")
                else:
                    try:
                        user = auth.create_first_admin(conn, username, display, pw, email=email)
                    except (ValueError, PermissionError) as e:
                        st.error(str(e))
                    else:
                        st.session_state["user_id"] = user.id
                        st.rerun()
        return

    st.subheader("ログイン")
    with st.form("login"):
        username = st.text_input("ユーザー ID")
        pw = st.text_input("パスワード", type="password")
        if st.form_submit_button("ログイン", type="primary"):
            user = auth.authenticate(conn, username, pw)
            if user is None:
                time.sleep(1)  # 総当たりを遅らせる
                st.error("ユーザー ID またはパスワードが違います")
            else:
                st.session_state["user_id"] = user.id
                st.rerun()


conn = get_conn()
user = current_user(conn)

if user is None:
    nav = st.navigation([st.Page(login_page, title="ログイン", icon="🔑", url_path="login")])
elif user.must_change_password:
    nav = st.navigation([st.Page("pages/9_アカウント.py", title="パスワード変更", icon="🔑", url_path="account")])
else:
    catalog_pages = [
        st.Page("pages/0_ホーム.py", title="ホーム", icon="📚", url_path="home", default=True),
        st.Page("pages/10_問い.py", title="問い", icon="❓", url_path="questions"),
        st.Page("pages/2_アセット詳細.py", title="アセット詳細", icon="📝", url_path="detail"),
        st.Page("pages/3_リネージ.py", title="リネージ", icon="🔀", url_path="lineage"),
        st.Page("pages/7_ドメイン.py", title="ドメイン", icon="🏷️", url_path="domains"),
        st.Page("pages/8_役割の棚卸し.py", title="役割の棚卸し", icon="🗂️", url_path="role-review"),
    ]
    if auth.can(user, Perm.MANAGE_ASSETS):
        catalog_pages.append(st.Page("pages/1_登録.py", title="登録", icon="➕", url_path="register"))
    sections = {"カタログ": catalog_pages}
    if auth.can(user, Perm.ADMIN):
        sections["管理"] = [
            st.Page("pages/4_プロパティ設定.py", title="プロパティ設定", icon="⚙️", url_path="properties"),
            st.Page("pages/5_ユーザー管理.py", title="ユーザー管理", icon="👥", url_path="users"),
            st.Page("pages/6_変更履歴.py", title="変更履歴", icon="🕘", url_path="history"),
        ]
    sections["アカウント"] = [st.Page("pages/9_アカウント.py", title="アカウント", icon="👤", url_path="account")]
    nav = st.navigation(sections)

if user is not None:
    with st.sidebar:
        st.markdown(f"**{user.display_name}**（{user.username}）")
        st.caption(f"ロール: {user.role_label}")
        if st.button("ログアウト"):
            logout()
            st.rerun()

nav.run()
