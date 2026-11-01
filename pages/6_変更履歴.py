"""全体の変更履歴（管理者のみ）。"""

from datetime import date, timedelta

import streamlit as st

from catalog import auth, changelog
from catalog.auth import Perm
from catalog.ui import asset_options, require_page

conn, user = require_page(Perm.ADMIN)

st.title("🕘 変更履歴")

c1, c2, c3 = st.columns([1, 1, 2])
usernames = [u["username"] for u in auth.list_users(conn)]
who = c1.selectbox("ユーザー", [None, *usernames], format_func=lambda v: "（すべて）" if v is None else v)
assets = asset_options(conn)
asset = c2.selectbox("アセット", [None, *assets], format_func=lambda v: "（すべて）" if v is None else assets[v])
period = c3.date_input("期間", value=(date.today() - timedelta(days=30), date.today()))
since, until = (period[0].isoformat(), period[1].isoformat()) if len(period) == 2 else (None, None)

rows = changelog.list_changes(conn, asset_id=asset, username=who, since=since, until=until, limit=1000)
st.caption(f"{len(rows)} 件（最大1000件。削除済みアセットの履歴は、アセットを「すべて」にすると表示されます）")
st.dataframe(changelog.to_display_rows(rows), hide_index=True, width="stretch")
