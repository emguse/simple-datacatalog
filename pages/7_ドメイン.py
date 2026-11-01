"""ドメイン: オーナー・スチュワードを設定する基本の単位。所属アセットは割り当てを継承する。

閲覧は全員、ドメインの作成・編集・削除と割り当ての変更は管理者のみ。
"""

import streamlit as st

from catalog import auth, roles
from catalog.auth import Perm
from catalog.ui import DETAIL_PAGE, assignments_table, require_page, role_editor

conn, user = require_page(Perm.VIEW)
is_admin = auth.can(user, Perm.ADMIN)

st.title("🏷️ ドメイン")
st.caption(
    "ドメインに設定したオーナー・スチュワードは、所属するアセットに継承されます。"
    "例外はアセットの詳細ページで個別に上書きします。"
)
if msg := st.session_state.pop("domain_flash", None):
    st.success(msg)


def flash(message: str) -> None:
    st.session_state["domain_flash"] = message
    st.rerun()


domains = roles.list_domains(conn)
if domains:
    st.dataframe(
        [{"ドメイン": d["name"], "説明": d["description"], "所属アセット数": d["asset_count"]} for d in domains],
        hide_index=True,
        width="stretch",
    )
else:
    st.info("ドメインはまだありません。" + ("下の「ドメインを追加」から作成してください。" if is_admin else ""))

if is_admin:
    with st.expander("ドメインを追加", expanded=not domains):
        with st.form("add_domain", clear_on_submit=True):
            name = st.text_input("ドメイン名", placeholder="例: 販売管理")
            desc = st.text_area("説明")
            if st.form_submit_button("追加", type="primary"):
                try:
                    roles.create_domain(conn, user, name, desc)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    flash(f"ドメイン「{name.strip()}」を追加しました")

if not domains:
    st.stop()

ids = [d["id"] for d in domains]
try:
    current = int(st.query_params.get("domain", ids[0]))
except ValueError:
    current = ids[0]
names = {d["id"]: d["name"] for d in domains}
domain_id = st.selectbox("ドメインを選択", ids, index=ids.index(current) if current in ids else 0, format_func=names.get)
st.query_params["domain"] = str(domain_id)
domain = roles.get_domain(conn, domain_id)

st.subheader(f"ドメイン: {domain['name']}")
if domain["description"]:
    st.write(domain["description"])

st.markdown("**役割の割り当て**")
own = roles.assignments_for(conn, domain_id=domain_id)
for a in own:
    a["stale"] = roles.is_stale(a["last_confirmed"])
if own:
    assignments_table(own)
else:
    st.caption("割り当てはありません。")
covered = {s for a in own if a["kind"] == "owner" for s in a["scopes"]}
if missing := [s for s in roles.OWNER_SCOPES if s not in covered]:
    st.warning("オーナーが決まっていない責任範囲: " + "、".join(missing))

st.markdown("**所属アセット**")
assets = conn.execute("SELECT id, name FROM assets WHERE domain_id = ? ORDER BY name", (domain_id,)).fetchall()
for a in assets:
    st.page_link(DETAIL_PAGE, label=a["name"], icon="📝", query_params={"id": a["id"]})
if not assets:
    st.caption("所属アセットはありません（アセットの詳細ページでドメインを設定します）。")

if is_admin:
    st.divider()
    with st.expander("割り当てを編集"):
        role_editor(conn, user, domain_id=domain_id)
    with st.expander("ドメインを編集・削除"):
        with st.form(f"edit_domain_{domain_id}"):
            new_name = st.text_input("ドメイン名", value=domain["name"])
            new_desc = st.text_area("説明", value=domain["description"])
            if st.form_submit_button("保存"):
                try:
                    roles.update_domain(conn, user, domain_id, name=new_name, description=new_desc)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    flash("ドメインを更新しました")
        st.caption(f"削除すると、所属アセット {len(assets)} 件はドメインなしになり、このドメインの割り当ては消えます。")
        if st.checkbox("削除してよいことを確認しました", key=f"del_domain_ck_{domain_id}"):
            if st.button("ドメインを削除", key=f"del_domain_{domain_id}"):
                roles.delete_domain(conn, user, domain_id)
                st.query_params.pop("domain", None)
                flash(f"ドメイン「{domain['name']}」を削除しました")
