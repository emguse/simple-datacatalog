"""問い: データが答えるべき問いを登録し、少しずつ育てる。

登録に必須なのは問いの文だけ。ほかの項目は「💡 次に書くとよいこと」を見ながら後から足していく。
閲覧は全員、登録・編集は編集者以上。
"""

from datetime import date

import streamlit as st

from catalog import auth
from catalog import questions as qs
from catalog.asset_status import ACTIVE
from catalog.auth import Perm
from catalog.ui import (
    DETAIL_PAGE,
    UNSET,
    FormSnapshot,
    asset_options,
    question_badges,
    require_page,
)

conn, user = require_page(Perm.VIEW)
can_edit = auth.can(user, Perm.EDIT)

st.title("❓ 問い")
st.caption(
    "データは問いに答えるために存在します。「何を知りたいか」を一文で書いておくと、"
    "使う人・責任を持つ人（オーナー）・守る人（スチュワード）がつながります。"
    "最初は問いの文だけで十分です。必要な品質や受諾は、あとから少しずつ育てていきましょう。"
)
if msg := st.session_state.pop("q_flash", None):
    st.success(msg)

assets = asset_options(conn)

# ---- 新しい問い
if can_edit:
    gen = st.session_state.setdefault("q_new_gen", 0)
    with st.container(border=True):
        text = st.text_input(
            "知りたいことは何ですか？",
            placeholder="例: " + " / ".join(qs.EXAMPLES[:2]),
            key=f"q_new_text_{gen}",
        )
        if text.strip():
            if not qs.looks_like_question(text):
                st.caption("💡 「〜か？」の形にすると、何を知りたいかが伝わりやすくなります（このままでも登録できます）。")
            similar = qs.similar_questions(conn, text)
            if similar:
                st.info("似た問いが既にあります。同じ内容なら、そちらにデータを紐づけるのもおすすめです。")
                for q in similar:
                    st.markdown(f"- **{q['code']}** {q['text']}　{question_badges(q)}")
            suggestions = qs.suggest_assets(conn, text)
            if suggestions:
                st.caption("使えそうなデータ: " + "、".join(f"{assets[s['id']]}（{'・'.join(s['matched'][:3])}）" for s in suggestions))
            # 非推奨のデータは候補に見せるが、最初から選んだ状態にはしない
            preselect = [s["id"] for s in suggestions if s["score"] >= 0.3 and s["status"] == ACTIVE]
            chosen = st.multiselect(
                "答えに使うデータ（あとからでも紐づけられます）",
                list(assets),
                default=preselect,
                format_func=assets.get,
                key=f"q_new_assets_{gen}_{hash(text)}",
            )
            kind = st.radio("種別（任意）", ["", "分析", "業務"], horizontal=True,
                            format_func=lambda k: k or "未定", key=f"q_new_kind_{gen}")
            if st.button("問いとして登録", type="primary", key=f"q_new_btn_{gen}"):
                try:
                    qid = qs.create_question(conn, user, text, asset_ids=chosen, kind=kind)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
                else:
                    st.session_state["q_new_gen"] = gen + 1
                    code = qs.get_question(conn, qid)["code"]
                    st.session_state["q_flash"] = f"{code} を登録しました。下の一覧から、必要な品質などを少しずつ書き足せます。"
                    st.query_params["q"] = str(qid)
                    st.rerun()
else:
    st.info("問いの登録は編集者以上が行えます。知りたいことがあれば、データのオーナーやスチュワードに伝えてください。")

# ---- 一覧
focus = st.query_params.get("q")
try:
    focus = int(focus) if focus else None
except ValueError:
    focus = None

with st.expander("絞り込み", expanded=False):
    c1, c2, c3, c4 = st.columns(4)
    query = c1.text_input("キーワード", placeholder="問い・データ名・品質")
    status = c2.selectbox("状態", [None, *qs.STATUSES], index=1, format_func=lambda v: v or "（すべて）")
    decision = c3.selectbox("受諾状態", [None, *qs.DECISIONS], format_func=lambda v: v or "（すべて）")
    kind_f = c4.selectbox("種別", [None, "分析", "業務"], format_func=lambda v: v or "（すべて）")
    c5, c6 = st.columns(2)
    review_due = c5.checkbox("見直し日を過ぎたもの")
    no_assets = c6.checkbox("データが未特定のもの")

rows = qs.list_questions(conn, query=query, status=status, decision=decision, kind=kind_f,
                         review_due=review_due, no_assets=no_assets)
if focus is not None:  # リンクから開いたときは、その問いを先頭に
    target = qs.get_question(conn, focus)
    rows = ([target] if target else []) + [q for q in rows if q["id"] != focus]
st.caption(f"{len(rows)} 件")
if not rows:
    st.caption("まだ問いはありません。例えば: " + " / ".join(qs.EXAMPLES))


def edit_form(q: dict) -> None:
    snap = FormSnapshot("question", q["id"], lambda: qs.get_question(conn, q["id"]))
    if snap.values is None:
        st.warning("この問いは削除されています。")
        return
    v = snap.values
    snap.stale_notice()
    linked = [a["id"] for a in v["assets"]]
    with st.form(snap.wkey("form")):
        text = st.text_input("問い ＊", value=v["text"], key=snap.wkey("text"))
        c1, c2, c3 = st.columns(3)
        kind = c1.selectbox("種別", qs.KINDS, index=qs.KINDS.index(v["kind"]), format_func=lambda k: k or "未定",
                            key=snap.wkey("kind"))
        dept = c2.text_input("持ち込んだ部門", value=v["requester_dept"], key=snap.wkey("dept"))
        requester = c3.text_input("依頼者", value=v["requester"], key=snap.wkey("requester"))
        chosen = st.multiselect("答えに使うデータ", list(assets), default=[i for i in linked if i in assets],
                                format_func=assets.get, key=snap.wkey("assets"))
        quality = st.text_area(
            "必要な品質", value=v["required_quality"], key=snap.wkey("quality"),
            placeholder="この問いに答えるのに必要な水準を、具体的に。\n"
                        "例: 明細単位で正確。月末締めの翌3営業日までに確定（鮮度・粒度・正確性・網羅性の観点で）",
        )
        st.markdown("**受諾**（オーナーが判断します）")
        c1, c2, c3 = st.columns(3)
        decision = c1.selectbox("受諾状態", qs.DECISIONS, index=qs.DECISIONS.index(v["decision"]),
                                key=snap.wkey("decision"))
        deciders = qs.suggested_deciders(conn, linked)
        opts = [UNSET, *dict.fromkeys([*deciders, *([v["decided_by"]] if v["decided_by"] else [])])]
        decided_by = c2.selectbox("受諾者", opts, index=opts.index(v["decided_by"]) if v["decided_by"] else 0,
                                  accept_new_options=True, key=snap.wkey("decided_by"),
                                  help="候補は、紐づけたデータで「問いの受諾」を受け持つオーナーです")
        decided_at = c3.date_input("受諾日", value=date.fromisoformat(v["decided_at"]) if v["decided_at"] else None,
                                   key=snap.wkey("decided_at"), help="受諾にすると、空なら今日が入ります")
        condition = st.text_input("受諾の条件（条件付き受諾のとき）", value=v["condition_note"], key=snap.wkey("cond"))
        st.markdown("**監視と見直し**")
        c1, c2, c3 = st.columns(3)
        monitors = qs.suggested_monitors(conn, linked)
        opts = [UNSET, *dict.fromkeys([*monitors, *([v["monitor"]] if v["monitor"] else [])])]
        monitor = c1.selectbox("監視担当", opts, index=opts.index(v["monitor"]) if v["monitor"] else 0,
                               accept_new_options=True, key=snap.wkey("monitor"),
                               help="候補は、紐づけたデータのスチュワードです")
        metric = c2.text_input("品質指標", value=v["quality_metric"], key=snap.wkey("metric"),
                               placeholder="例: 受注入力の遅延件数")
        review = c3.date_input("見直し日", value=date.fromisoformat(v["review_date"]) if v["review_date"] else None,
                               key=snap.wkey("review"), help="空なら「半年後にする」ボタンで入れられます")
        status = st.radio("状態", qs.STATUSES, index=qs.STATUSES.index(v["status"]), horizontal=True,
                          key=snap.wkey("status"))
        if st.form_submit_button("保存", type="primary"):
            inputs = {
                "text": text, "kind": kind, "requester_dept": dept, "requester": requester,
                "required_quality": quality, "decision": decision,
                "decided_by": "" if decided_by in (None, UNSET) else decided_by,
                "decided_at": decided_at.isoformat() if decided_at else "",
                "condition_note": condition, "monitor": "" if monitor in (None, UNSET) else monitor,
                "quality_metric": metric, "review_date": review.isoformat() if review else "", "status": status,
            }
            changed = {f: val for f, val in inputs.items() if str(val).strip() != v[f]}
            try:
                if changed:
                    qs.update_question(conn, user, q["id"], **changed)
                qs.link_assets(conn, user, q["id"], add=[i for i in chosen if i not in linked],
                               remove=[i for i in linked if i not in chosen])
            except (ValueError, PermissionError) as e:
                st.error(str(e))
            else:
                snap.reset()
                st.session_state["q_flash"] = f"{v['code']} を保存しました"
                st.rerun()
    if not v["review_date"] and v["status"] == "有効":
        if st.button(f"見直し日を半年後（{qs.default_review_date()}）にする", key=snap.wkey("review_btn")):
            qs.update_question(conn, user, q["id"], review_date=qs.default_review_date())
            snap.reset()
            st.rerun()


def details(q: dict) -> None:
    fields = [f for f in qs.FIELD_LABELS if f != "text" and q[f]]
    st.dataframe([{"項目": qs.FIELD_LABELS[f], "内容": q[f]} for f in fields], hide_index=True, width="stretch")


for q in rows:
    with st.container(border=True):
        st.markdown(f"**{q['code']}**　{q['text']}　{question_badges(q)}")
        if q["assets"]:
            cols = st.columns(min(len(q["assets"]), 4))
            for i, a in enumerate(q["assets"]):
                cols[i % len(cols)].page_link(DETAIL_PAGE, label=a["name"], icon="📝", query_params={"id": a["id"]})
        for h in qs.hints(q):
            st.caption(f"💡 {h}")
        with st.expander("詳しく見る・育てる" if can_edit else "詳しく見る", expanded=q["id"] == focus):
            if can_edit:
                edit_form(q)
            else:
                details(q)
