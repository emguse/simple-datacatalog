"""ホーム: アセット一覧と検索。"""

import streamlit as st

from catalog import auth, roles
from catalog import properties as props
from catalog import repository as repo
from catalog.asset_status import ACTIVE, ASSET_STATUSES, DEPRECATED, RETIRED
from catalog.auth import Perm
from catalog.ui import (
    DETAIL_PAGE,
    LINEAGE_PAGE,
    REGISTER_PAGE,
    asset_options,
    asset_status_badge,
    completeness_badge,
    require_page,
)

st.title("📚 Simple Data Catalog")

conn, user = require_page(Perm.VIEW)
defs = props.list_definitions(conn)
by_key = {d.builtin_key: d for d in defs if d.builtin}
# カードに1行で表示する主要プロパティ（名前は設定で変えられるので builtin_key で引く）
HIGHLIGHT = [by_key[k] for k in ("update_frequency", "visibility") if k in by_key]

c1, c2 = st.columns([2, 1])
query = c1.text_input(
    "キーワード検索",
    placeholder="知りたいこと（例: 粗利、出荷済み）・アセット名・カラム名・オーナーなど",
    help="問いの文も検索するので、知りたいことの言葉で探すと、その問いに答えるデータが見つかります",
)
tags = c2.multiselect("タグで絞り込み（すべてを含む）", repo.list_tags(conn))

c1, c2, c3 = st.columns([2, 1, 1])
domain_names = {d["id"]: d["name"] for d in roles.list_domains(conn)}
domain_ids = [  # 0 =「ドメインなし」（repo には None で渡す）
    i or None
    for i in c1.multiselect("ドメイン", [*domain_names, 0], format_func=lambda i: domain_names.get(i, "（ドメインなし）"))
]
incomplete_only = c2.checkbox("未入力の必須項目があるものだけ")
stale_roles_only = c3.checkbox("要確認の役割があるものだけ", help=f"最終確認から {roles.STALE_DAYS} 日を過ぎた割り当て")
c1, c2 = st.columns(2)
statuses = c1.multiselect(
    "ステータス",
    ASSET_STATUSES,
    default=[ACTIVE, DEPRECATED],
    help="既定では廃止したものを隠しています。空にするとすべて表示します",
)
no_questions_only = c2.checkbox(
    "答える問いが未登録のものだけ",
    help="どの問いにも使われていないデータは、維持をやめる候補かもしれません。まずは問いを書いてみましょう",
)
property_filters: dict[int, list[str]] = {}
select_defs = [d for d in defs if d.type == "select"]
with st.expander("プロパティで絞り込み"):
    cols = st.columns(min(len(select_defs), 4) or 1)
    for i, d in enumerate(select_defs):
        # 選択肢の順に並べ、選択肢から外された古い値も末尾に出す
        options = d.choices + [v for v in props.distinct_values(conn, d.id) if v not in d.choices]
        property_filters[d.id] = cols[i % len(cols)].multiselect(d.name, options, key=f"pf_{d.id}")

assets = repo.list_assets(
    conn,
    query=query,
    tags=tags,
    property_filters=property_filters,
    incomplete_only=incomplete_only,
    domain_ids=domain_ids,
    stale_roles_only=stale_roles_only,
    no_questions_only=no_questions_only,
    statuses=statuses,
)
complete = sum(a["completeness"].complete for a in assets if a["status"] != RETIRED)
st.caption(f"{len(assets)} 件（必須項目がすべて入力済み: {complete} 件）")

if not assets:
    # 絞り込みの有無ではなく実際の件数で判定する（既定で廃止を隠すので、全件廃止でも結果が空になる）
    registered = bool(asset_options(conn))
    if not registered:
        st.info("まだアセットが登録されていません。")
        if auth.can(user, Perm.MANAGE_ASSETS):
            st.page_link(REGISTER_PAGE, label="登録ページへ", icon="➕")
    elif statuses and RETIRED not in statuses:
        st.info("該当するアセットはありません（廃止したものは、ステータスの絞り込みで表示できます）。")
    else:
        st.info("該当するアセットはありません。")

for a in assets:
    comp = a["completeness"]
    with st.container(border=True):
        head, links = st.columns([4, 1])
        head.markdown(f"### {a['name']}")
        last = a["last_update"]
        updated = f"更新 {last['at'][:16]}（{last['username'] or 'システム'}）" if last else f"更新 {a['updated_at']}"
        head.caption(f"{a['format']} ・ {a['row_count']:,} 行 ・ {a['column_count']} カラム ・ {updated}")
        n_q = a["question_count"]
        q_badge = f":violet-badge[❓ 答える問い {n_q}件]" if n_q else ":gray-badge[問い未登録]"
        retired = a["status"] == RETIRED
        badges = [asset_status_badge(a["status"]), "" if retired else completeness_badge(comp), q_badge]
        badges += [f":blue-badge[{t}]" for t in a["tags"]]
        head.markdown(" ".join(b for b in badges if b))
        head.markdown(
            f"**ドメイン**: {a['domain_name'] or '—'} ・ **オーナー**: {'、'.join(a['owners']) or '—'} ・ "
            f"**スチュワード**: {'、'.join(a['stewards']) or '—'}"
            + (f" ・ :orange[⚠ 要確認 {a['stale_roles']} 件]" if a["stale_roles"] else "")
        )
        head.markdown(" ・ ".join(f"**{d.name}**: {a['properties'].get(d.name, '—')}" for d in HIGHLIGHT))
        head.write(a["description"] or "_（説明なし）_")
        if comp.missing and not retired:
            head.caption("未入力: " + "、".join(comp.missing))
        links.page_link(DETAIL_PAGE, label="詳細", icon="📝", query_params={"id": a["id"]})
        links.page_link(LINEAGE_PAGE, label="リネージ", icon="🔀", query_params={"id": a["id"]})
