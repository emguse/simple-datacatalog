"""指標: ビジネスプロセスから導く数字の定義を登録し、少しずつ育てる。

登録に必須なのは名前だけ。ほかの項目は「💡 次に書くとよいこと」を見ながら後から足していく。
カラム単位の計算式は、これまでどおりアセット／カラムの説明に書く（ここでは文章の計算式だけ持つ）。
閲覧は全員、指標の登録・編集は編集者以上、ビジネスプロセスの管理は管理者・パワーユーザー。
"""

import streamlit as st

from catalog import auth
from catalog import business_processes as bps
from catalog import metrics as ms
from catalog import questions as qs
from catalog.auth import Perm
from catalog.ui import (
    DETAIL_PAGE,
    QUESTIONS_PAGE,
    UNSET,
    FormSnapshot,
    asset_options,
    require_page,
)

conn, user = require_page(Perm.VIEW)
can_edit = auth.can(user, Perm.EDIT)
can_manage = auth.can(user, Perm.MANAGE_ASSETS)

st.title("📈 指標")
st.caption(
    "指標は「どの業務（ビジネスプロセス）から生まれる、どんな数字か」の定義です。"
    "名前・定義・計算式・単位・集計粒度を書き、元になるデータと答える問いを紐づけます。"
    "最初は名前だけで十分です。カラム単位の計算式は、アセットやカラムの説明に書いてください。"
)
if msg := st.session_state.pop("m_flash", None):
    st.success(msg)

assets = asset_options(conn)
bp_list = bps.list_business_processes(conn)
bp_names = {b["id"]: b["name"] for b in bp_list}
BP_NONE = 0  # 「未設定」は None ではなく 0
bp_choices = [BP_NONE, *bp_names]
questions_map = {q["id"]: f"{q['code']} {q['text']}" for q in qs.list_questions(conn)}


def bp_label(i: int) -> str:
    return bp_names[i] if i else "（未設定）"


# ---- ビジネスプロセスの管理
with st.expander(f"ビジネスプロセス（{len(bp_list)}）"):
    st.caption("受注・出荷・請求など、指標の起点になる業務の流れです。ドメイン（オーナーを継承する単位）とは別のものです。")
    if bp_list:
        st.dataframe(
            [{"名前": b["name"], "説明": b["description"], "指標数": b["metric_count"]} for b in bp_list],
            hide_index=True, width="stretch",
        )
    else:
        st.caption("まだありません。例: 受注 / 出荷 / 請求")
    if can_manage:
        gen = st.session_state.setdefault("bp_gen", 0)
        with st.form(f"bp_new_{gen}"):
            c1, c2 = st.columns([1, 2])
            n = c1.text_input("名前")
            d = c2.text_area("説明（任意）", height=80)
            if st.form_submit_button("追加") and n.strip():
                try:
                    bps.create_business_process(conn, user, n, d)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    st.session_state["bp_gen"] = gen + 1
                    st.session_state["m_flash"] = f"ビジネスプロセス「{n.strip()}」を追加しました"
                    st.rerun()
        if bp_list:
            # 選択はフォームの外に置く（フォーム内だと選び直しても入力欄が切り替わらず、保存時に別の行の値で上書きされる）
            target = st.selectbox("編集・削除するビジネスプロセス", list(bp_names), format_func=bp_names.get,
                                  key="bp_edit_target")
            cur = next(b for b in bp_list if b["id"] == target)
            with st.form(f"bp_edit_{gen}_{target}"):
                new_name = st.text_input("名前", value=cur["name"], key=f"bp_name_{target}")
                new_desc = st.text_area("説明", value=cur["description"], key=f"bp_desc_{target}", height=100)
                c1, c2 = st.columns(2)
                save = c1.form_submit_button("保存")
                delete = c2.form_submit_button(f"削除（指標 {cur['metric_count']} 件は未設定に戻ります）")
                if save or delete:
                    try:
                        if save:
                            changed = {f: v for f, v in (("name", new_name), ("description", new_desc))
                                       if v.strip() != cur[f]}
                            if changed:
                                bps.update_business_process(conn, user, target, **changed)
                            st.session_state["m_flash"] = "保存しました"
                        else:
                            bps.delete_business_process(conn, user, target)
                            st.session_state.pop("bp_edit_target", None)  # 消えた行を選択したままにしない
                            st.session_state["m_flash"] = f"「{cur['name']}」を削除しました"
                    except (ValueError, PermissionError) as e:
                        st.error(str(e))
                    else:
                        st.rerun()
    else:
        st.caption("ビジネスプロセスの追加・編集はパワーユーザー以上が行えます。")

# ---- 新しい指標
if can_edit:
    gen = st.session_state.setdefault("m_new_gen", 0)
    with st.container(border=True):
        name = st.text_input("指標の名前", placeholder="例: " + " / ".join(ms.EXAMPLES[:2]), key=f"m_new_name_{gen}")
        if name.strip():
            c1, c2 = st.columns(2)
            bp = c1.selectbox("ビジネスプロセス（任意）", bp_choices, format_func=bp_label, key=f"m_new_bp_{gen}")
            chosen = c2.multiselect("元になるデータ（任意）", list(assets), format_func=assets.get,
                                    key=f"m_new_assets_{gen}")
            if st.button("指標として登録", type="primary", key=f"m_new_btn_{gen}"):
                try:
                    mid = ms.create_metric(conn, user, name, asset_ids=chosen, business_process_id=bp)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    st.session_state["m_new_gen"] = gen + 1
                    code = ms.get_metric(conn, mid)["code"]
                    st.session_state["m_flash"] = f"{code} を登録しました。下の一覧から、定義や計算式を書き足せます。"
                    st.query_params["m"] = str(mid)
                    st.rerun()
else:
    st.info("指標の登録は編集者以上が行えます。")

# ---- 一覧
focus = st.query_params.get("m")
try:
    focus = int(focus) if focus else None
except ValueError:
    focus = None

with st.expander("絞り込み", expanded=False):
    c1, c2, c3 = st.columns(3)
    query = c1.text_input("キーワード", placeholder="名前・定義・計算式・データ名")
    bp_f = c2.selectbox("ビジネスプロセス", [None, *bp_names], format_func=lambda i: bp_names[i] if i else "（すべて）")
    no_assets = c3.checkbox("元データが未特定のもの")

rows = ms.list_metrics(conn, query=query, business_process_id=bp_f, no_assets=no_assets)
if focus is not None:  # リンクから開いたときは、その指標を先頭に
    target_m = ms.get_metric(conn, focus)
    rows = ([target_m] if target_m else []) + [m for m in rows if m["id"] != focus]
st.caption(f"{len(rows)} 件")
if not rows:
    st.caption("まだ指標はありません。例えば: " + " / ".join(ms.EXAMPLES))


def free_text_select(label: str, field: str, current: str, key: str):
    """単位・集計粒度: 登録済みの値をサジェストしつつ、新しい値も入力できる。"""
    opts = [UNSET, *dict.fromkeys([*ms.suggested_values(conn, field), *([current] if current else [])])]
    return st.selectbox(label, opts, index=opts.index(current) if current else 0, accept_new_options=True, key=key)


def edit_form(m: dict) -> None:
    snap = FormSnapshot("metric", m["id"], lambda: ms.get_metric(conn, m["id"]))
    if snap.values is None:
        st.warning("この指標は削除されています。")
        return
    v = snap.values
    snap.stale_notice()
    linked_a = [a["id"] for a in v["assets"]]
    linked_q = [q["id"] for q in v["questions"]]
    cur_bp = v["business_process_id"] or BP_NONE
    with st.form(snap.wkey("form")):
        name = st.text_input("名前 ＊", value=v["name"], key=snap.wkey("name"))
        bp = st.selectbox("ビジネスプロセス", bp_choices,
                          index=bp_choices.index(cur_bp) if cur_bp in bp_choices else 0,
                          format_func=bp_label, key=snap.wkey("bp"))
        desc = st.text_area("定義（Markdown が使えます）", value=v["description"], key=snap.wkey("desc"), height=160,
                            placeholder="何を表す数字か。含めるもの・除くものも書くとずれにくくなります。")
        formula = st.text_area("計算式（Markdown が使えます）", value=v["formula"], key=snap.wkey("formula"), height=120,
                               placeholder="文章で。例: 受注明細の（数量 × 単価）を、キャンセルを除いて合計する")
        c1, c2 = st.columns(2)
        with c1:
            unit = free_text_select("単位", "unit", v["unit"], snap.wkey("unit"))
        with c2:
            grain = free_text_select("集計粒度", "grain", v["grain"], snap.wkey("grain"))
        chosen_a = st.multiselect("元になるデータ", list(assets), default=[i for i in linked_a if i in assets],
                                  format_func=assets.get, key=snap.wkey("assets"))
        chosen_q = st.multiselect("答える問い", list(questions_map),
                                  default=[i for i in linked_q if i in questions_map],
                                  format_func=questions_map.get, key=snap.wkey("questions"))
        if st.form_submit_button("保存", type="primary"):
            inputs = {
                "name": name, "business_process_id": bp or None, "description": desc, "formula": formula,
                "unit": "" if unit in (None, UNSET) else unit, "grain": "" if grain in (None, UNSET) else grain,
            }
            changed = {f: val for f, val in inputs.items()
                       if (val if f == "business_process_id" else str(val).strip()) != v[f]}
            try:
                if changed:
                    # 外す操作は None ではなく 0 で渡す（None は「変更なし」と区別できないため）
                    if "business_process_id" in changed:
                        changed["business_process_id"] = changed["business_process_id"] or BP_NONE
                    ms.update_metric(conn, user, m["id"], **changed)
                ms.link_assets(conn, user, m["id"], add=[i for i in chosen_a if i not in linked_a],
                               remove=[i for i in linked_a if i not in chosen_a])
                ms.link_questions(conn, user, m["id"], add=[i for i in chosen_q if i not in linked_q],
                                  remove=[i for i in linked_q if i not in chosen_q])
            except (ValueError, PermissionError) as e:
                st.error(str(e))
            else:
                snap.reset()
                st.session_state["m_flash"] = f"{v['code']} を保存しました"
                st.rerun()


def show_text(m: dict) -> None:
    """定義と計算式を Markdown として読みやすく表示する（改行・箇条書きを活かす）。"""
    if m["description"]:
        st.markdown(f"**定義**\n\n{m['description']}")
    if m["formula"]:
        st.markdown(f"**計算式**\n\n{m['formula']}")


def details(m: dict) -> None:
    """閲覧者向け。定義・計算式はカードに出ているので、残りの項目だけ。"""
    rows_ = [{"項目": ms.FIELD_LABELS[f], "内容": m[f]} for f in ("unit", "grain") if m[f]]
    if rows_:
        st.dataframe(rows_, hide_index=True, width="stretch")
    else:
        st.caption("単位・集計粒度は未記入です。")


for m in rows:
    with st.container(border=True):
        badges = f"　:blue-badge[{m['business_process']}]" if m["business_process"] else ""
        unit = f"　:gray-badge[{m['unit']}]" if m["unit"] else ""
        st.markdown(f"**{m['code']}**　{m['name']}{badges}{unit}")
        if m["assets"]:
            cols = st.columns(min(len(m["assets"]), 4))
            for i, a in enumerate(m["assets"]):
                cols[i % len(cols)].page_link(DETAIL_PAGE, label=a["name"], icon="📝", query_params={"id": a["id"]})
        if m["questions"]:
            cols = st.columns(min(len(m["questions"]), 4))
            for i, q in enumerate(m["questions"]):
                cols[i % len(cols)].page_link(QUESTIONS_PAGE, label=q["code"], icon="❓", query_params={"q": q["id"]})
        show_text(m)
        for h in ms.hints(m):
            st.caption(f"💡 {h}")
        with st.expander("詳しく見る・育てる" if can_edit else "詳しく見る", expanded=m["id"] == focus):
            if can_edit:
                edit_form(m)
            else:
                details(m)
