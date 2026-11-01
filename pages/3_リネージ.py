"""リネージグラフ表示。"""

import streamlit as st

from catalog import lineage
from catalog import questions as qs
from catalog import repository as repo
from catalog.auth import Perm
from catalog.ui import DETAIL_PAGE, require_page, selected_asset_id

st.title("🔀 リネージ")

conn, user = require_page(Perm.VIEW)
c1, c2 = st.columns([3, 1])
with c1:
    asset_id = selected_asset_id(conn, user, "中心アセット")
if asset_id is None:
    st.stop()
depth = c2.slider("たどる深さ", min_value=1, max_value=5, value=2)

graph = lineage.subgraph(conn, asset_id, depth)
counts = qs.active_question_counts(conn)
non_active = repo.non_active_statuses(conn)
notes = {
    i: " ".join(n for n in (non_active.get(i), f"❓{counts[i]}" if counts.get(i) else None) if n)
    for i in graph.nodes
}
st.graphviz_chart(lineage.to_dot(graph, asset_id, notes), width="stretch")
st.caption(
    "❓N は、そのデータが答えている問いの数です。上流のデータは、下流を通じてそれらの問いを支えています。"
    "運用中でないデータには、ステータス（非推奨・廃止）を添えています。"
)

ups = lineage.reachable(conn, asset_id, "up", depth)
downs = lineage.reachable(conn, asset_id, "down", depth)
l, r = st.columns(2)
with l:
    st.markdown(f"**上流（{len(ups)}）**")
    for i in sorted(ups, key=graph.nodes.get):
        st.page_link(DETAIL_PAGE, label=graph.nodes[i], query_params={"id": i})
with r:
    st.markdown(f"**下流（{len(downs)}）**")
    for i in sorted(downs, key=graph.nodes.get):
        st.page_link(DETAIL_PAGE, label=graph.nodes[i], query_params={"id": i})
if not ups and not downs:
    st.info("リネージが登録されていません。アセット詳細ページで上流・下流を登録できます。")
