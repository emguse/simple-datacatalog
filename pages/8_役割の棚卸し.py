"""役割の棚卸し: すべての割り当てを横断して確認する（半年ごと・人事異動の後。docs §5.2）。"""

import streamlit as st

from catalog import auth, roles
from catalog import questions as qs
from catalog.auth import Perm
from catalog.ui import QUESTIONS_PAGE, assignments_table, question_line, require_page

conn, user = require_page(Perm.VIEW)

st.title("🗂️ 役割の棚卸し")
st.caption(
    f"最終確認日から {roles.STALE_DAYS} 日を過ぎた割り当ては「要確認（⚠）」になります。"
    "担当者が正しいことを確認したら「確認済みにする」で最終確認日を更新してください。"
)

c1, c2, c3 = st.columns([1, 1, 1])
stale_only = c1.checkbox("要確認のものだけ", value=True)
org = c2.selectbox("組織", [None, *roles.suggestions(conn, "org")], format_func=lambda v: "（すべて）" if v is None else v)
people = sorted(set(roles.suggestions(conn, "person")) | set(roles.suggestions(conn, "position")))
person = c3.selectbox("役職・担当者", [None, *people], format_func=lambda v: "（すべて）" if v is None else v,
                      help="人事異動の後は、異動した人や役職で絞り込んで確認します")

# ---- 見直し日を過ぎた問い（docs §5.2: 問いがまだ有効か、必要な品質は変わっていないか）
due = qs.list_questions(conn, review_due=True)
if due:
    with st.container(border=True):
        st.markdown(f"**見直し日を過ぎた問い（{len(due)}）**")
        st.caption("まだ有効か、必要な品質は変わっていないかを確かめましょう。不要になった問いは「廃止」にします。")
        for q in due:
            question_line(q)

st.subheader("役割の割り当て")
rows = roles.list_all_assignments(conn, stale_only=stale_only, org=org, person=person)
st.caption(f"{len(rows)} 件")
if not rows:
    st.success("該当する割り当てはありません。")
    st.stop()
assignments_table(rows, show_target=True)

# 確認済みにする（ドメインの割り当ては管理者、アセットの割り当てはパワーユーザー以上）
confirmable = [
    a for a in rows
    if auth.can(user, Perm.ADMIN if a["domain_id"] is not None else Perm.MANAGE_ASSETS)
]
if confirmable:
    def label(a):
        target = f"ドメイン {a['domain_name']}" if a["domain_id"] is not None else f"アセット {a['asset_name']}"
        return f"{target} / {a['role_label']}: {roles.assignment_label(a)}（最終確認 {a['last_confirmed']}）"

    with st.form("confirm_roles"):
        chosen = st.multiselect("確認済みにする割り当て", [a["id"] for a in confirmable],
                                format_func={a["id"]: label(a) for a in confirmable}.get)
        if st.form_submit_button("確認済みにする（最終確認日を今日に）", type="primary") and chosen:
            for aid in chosen:
                roles.confirm_assignment(conn, user, aid)
            st.toast(f"{len(chosen)} 件を確認済みにしました")
            st.rerun()
else:
    st.caption("確認済みにするには、パワーユーザー（アセット）または管理者（ドメイン）の権限が必要です。")
