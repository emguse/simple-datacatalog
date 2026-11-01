"""アセット詳細: 説明・タグ・プロパティ・カラム・リネージの編集、データ更新、変更履歴、プレビュー。

同時編集への対策として、各フォームは「開いた時点の値（スナップショット）」と入力値を比べ、
変えた項目だけを保存する。別々の項目を同時に編集しても互いに消し合わない。
"""

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from catalog import auth, changelog, roles
from catalog import questions as qs
from catalog import properties as props
from catalog import repository as repo
from catalog.auth import Perm
from catalog.ingest import SUPPORTED_EXTENSIONS, infer_schema, preview
from catalog.ui import (
    DOMAIN_PAGE,
    LINEAGE_PAGE,
    QUESTIONS_PAGE,
    FormSnapshot,
    asset_options,
    assignments_table,
    completeness_badge,
    property_input,
    question_line,
    require_page,
    review_changes,
    role_editor,
    schema_table,
    selected_asset_id,
    discard_upload,
    stage_upload,
    store_upload,
)

st.title("📝 アセット詳細")

conn, user = require_page(Perm.VIEW)
can_edit = auth.can(user, Perm.EDIT)
can_manage = auth.can(user, Perm.MANAGE_ASSETS)

asset_id = selected_asset_id(conn, user)
if asset_id is None:
    st.stop()

asset = repo.get_asset(conn, asset_id)
if asset is None:  # 他のユーザーが削除した直後など
    st.warning("このアセットは削除されました。")
    st.stop()

last = changelog.last_update(conn, asset_id)
st.caption(
    f"{asset['format']} ・ {asset['row_count']:,} 行 ・ `{asset['file_path']}` ・ 登録 {asset['created_at']} ・ "
    + (f"最終更新 {last['at'][:16]}（{last['username'] or 'システム'}）" if last else f"更新 {asset['updated_at']}")
)
if not can_edit:
    st.info("閲覧専用です（編集には「編集者」以上の権限が必要です）。")

flash_key = f"detail_flash_{asset_id}"
if msg := st.session_state.pop(flash_key, None):
    st.success(msg)


def saved(message: str, *snaps: FormSnapshot) -> None:
    for s in snaps:
        s.reset()
    st.session_state[flash_key] = message
    st.rerun()


# ---- 充実度（必須プロパティ＋説明）
comp = props.completeness(conn, asset_id)
c1, c2 = st.columns([1, 3])
c1.markdown(completeness_badge(comp))
c1.progress(comp.filled / comp.total)
if comp.missing:
    c2.warning("未入力の必須項目: " + "、".join(comp.missing))
else:
    c2.success("必須項目はすべて入力済みです。")

# ---- アセットの説明・タグ
st.subheader("アセット")
asset_snap = FormSnapshot(
    "asset",
    asset_id,
    lambda: {"description": repo.get_asset(conn, asset_id)["description"], "tags": repo.asset_tags(conn, asset_id)},
)
if can_edit:
    asset_snap.stale_notice()
    with st.form(asset_snap.wkey("form")):
        desc = st.text_area("説明", value=asset_snap.values["description"], key=asset_snap.wkey("desc"))
        tags = st.multiselect(
            "タグ（新しいタグは入力して Enter）",
            sorted(set(repo.list_tags(conn)) | set(asset_snap.values["tags"])),
            default=asset_snap.values["tags"],
            accept_new_options=True,
            key=asset_snap.wkey("tags"),
        )
        if st.form_submit_button("保存", type="primary"):
            before = asset_snap.values
            try:
                repo.update_asset_metadata(
                    conn,
                    user,
                    asset_id,
                    description=desc if desc != before["description"] else None,
                    add_tags=[t for t in tags if t not in before["tags"]],
                    remove_tags=[t for t in before["tags"] if t not in tags],
                )
            except (ValueError, PermissionError) as e:
                st.error(str(e))
            else:
                saved("アセット情報を保存しました", asset_snap)
else:
    st.write(asset["description"] or "_（説明なし）_")
    if tags := repo.asset_tags(conn, asset_id):
        st.markdown(" ".join(f":blue-badge[{t}]" for t in tags))

# ---- このデータで答える問い
st.subheader("このデータで答える問い")
linked = qs.questions_for_asset(conn, asset_id)
active = [q for q in linked if q["status"] == "有効"]
for q in active:
    question_line(q)
if not active:
    st.info(
        "このデータで、どんな問いに答えられますか？ 一つ書いておくと、使う人と守る人がつながります。\n\n"
        "例: " + " / ".join(qs.EXAMPLES[:3])
    )
if retired := [q for q in linked if q["status"] == "廃止"]:
    with st.expander(f"廃止した問い（{len(retired)}）"):
        for q in retired:
            question_line(q)
if can_edit:
    gen = st.session_state.setdefault(f"q_inline_gen_{asset_id}", 0)
    with st.form(f"q_inline_{asset_id}_{gen}"):
        c1, c2 = st.columns([4, 1])
        new_q = c1.text_input("このデータで答えられる問いを書く", placeholder=qs.EXAMPLES[0],
                              label_visibility="collapsed")
        if c2.form_submit_button("問いを追加") and new_q.strip():
            qid = qs.create_question(conn, user, new_q, asset_ids=[asset_id])
            st.session_state[f"q_inline_gen_{asset_id}"] = gen + 1
            hint = "" if qs.looks_like_question(new_q) else "（💡 「〜か？」の形にすると、より伝わりやすくなります）"
            saved(f"問い {qs.get_question(conn, qid)['code']} を追加しました。問いページで必要な品質などを書き足せます{hint}")
    others = {q["id"]: f"{q['code']} {q['text']}" for q in qs.list_questions(conn, status="有効")
              if q["id"] not in {x["id"] for x in linked}}
    if others:
        with st.expander("既存の問いにこのデータを紐づける"):
            with st.form(f"q_link_{asset_id}"):
                pick = st.multiselect("問い", list(others), format_func=others.get)
                if st.form_submit_button("紐づける") and pick:
                    for qid in pick:
                        qs.link_assets(conn, user, qid, add=[asset_id])
                    saved(f"{len(pick)} 件の問いに紐づけました")
if downstream := qs.downstream_questions(conn, asset_id):
    with st.expander(f"下流のデータを通じて支えている問い（{len(downstream)}）"):
        st.caption("このデータを元にした下流のデータが、次の問いに答えています。変更するときは影響に気をつけましょう。")
        for q in downstream:
            st.markdown(f"- **{q['code']}** {q['text']}　← {'、'.join(q['via'])}")
st.page_link(QUESTIONS_PAGE, label="問いの一覧へ", icon="❓")

# ---- プロパティ
st.subheader("プロパティ")
defs = props.list_definitions(conn)
prop_snap = FormSnapshot("props", asset_id, lambda: props.get_asset_properties(conn, asset_id))


def as_text(raw) -> str | None:
    if isinstance(raw, date):
        raw = raw.isoformat()
    text = "" if raw is None else str(raw).strip()
    return text or None


if can_edit:
    st.caption("＊ は必須項目です（未入力でも保存できますが、充実度に反映されます）。")
    prop_snap.stale_notice()
    with st.form(prop_snap.wkey("form")):
        inputs: dict[int, object] = {}
        cols = st.columns(2)
        for i, d in enumerate([d for d in defs if d.type != "longtext"]):
            # 定義（型・選択肢）が変わったらウィジェットを作り直す
            key = prop_snap.wkey(d.id, d.type, hash(tuple(d.choices)))
            with cols[i % 2]:
                inputs[d.id] = property_input(d, prop_snap.values.get(d.id), key, props.distinct_values(conn, d.id))
        for d in defs:
            if d.type == "longtext":
                inputs[d.id] = property_input(d, prop_snap.values.get(d.id), prop_snap.wkey(d.id, d.type))
        if st.form_submit_button("プロパティを保存", type="primary"):
            changed = {pid: raw for pid, raw in inputs.items() if as_text(raw) != prop_snap.values.get(pid)}
            errors = props.set_asset_properties(conn, user, asset_id, changed)
            for e in errors:
                st.error(e)
            if not errors:
                saved(f"プロパティを保存しました（{len(changed)} 項目）", prop_snap)
else:
    values = props.get_asset_properties(conn, asset_id)
    st.dataframe(
        [{"項目": d.name + (" ＊" if d.required else ""), "値": values.get(d.id, "")} for d in defs],
        hide_index=True,
        width="stretch",
    )

# ---- 役割（オーナー／スチュワード）
st.subheader("役割（オーナー／スチュワード）")
st.caption(
    "オーナーは問いを引き受けて答えられることに責任を持ち（役職で設定）、スチュワードは問いに答えられる状態を保ちます"
    "（人で設定）。ドメインのオーナー・スチュワードを継承し、例外だけをこのアセットで上書きします。"
    "オーナーは責任の範囲ごとに、スチュワードは種類（業務／技術）ごとに上書きされます。"
)
domains = {d["id"]: d["name"] for d in roles.list_domains(conn)}
if can_manage:
    # 「なし」は 0 で表す（selectbox の None は「未選択」と紛らわしいため）
    ids = [0, *domains]
    current_domain = asset["domain_id"] or 0
    chosen = st.selectbox(
        "ドメイン",
        ids,
        index=ids.index(current_domain) if current_domain in ids else 0,
        format_func=lambda i: domains.get(i, "（なし）"),
        key=f"domain_{asset_id}_{current_domain}",
    )
    if chosen != current_domain:
        chosen = chosen or None
        roles.set_asset_domain(conn, user, asset_id, chosen)
        saved(f"ドメインを「{domains.get(chosen, 'なし')}」に変更しました")
else:
    st.markdown(f"**ドメイン**: {domains.get(asset['domain_id'], '（なし）')}")
if asset["domain_id"] is not None:
    st.page_link(DOMAIN_PAGE, label=f"ドメイン「{domains.get(asset['domain_id'])}」を開く", icon="🏷️",
                 query_params={"domain": asset["domain_id"]})

effective = roles.effective_assignments(conn, asset_id)
if effective:
    assignments_table(effective, show_source=True)
else:
    st.caption("割り当てはありません。")
if uncovered := roles.uncovered_owner_scopes(effective):
    st.warning("オーナーが決まっていない責任範囲: " + "、".join(uncovered))
if not roles.has_steward(effective):
    st.warning("スチュワードが割り当てられていません。")
if any(a["stale"] for a in effective):
    st.warning(f"最終確認から {roles.STALE_DAYS} 日を過ぎた割り当てがあります（⚠）。担当者が正しいか確認してください。")
if can_manage:
    with st.expander("このアセットの割り当てを編集（個別設定）"):
        st.caption("ドメインから継承している割り当ては、ドメインのページで変更します（管理者）。")
        role_editor(conn, user, asset_id=asset_id)

# ---- カラム
st.subheader("カラム")
col_snap = FormSnapshot(
    "columns",
    asset_id,
    lambda: [
        {"id": c["id"], "カラム": c["name"], "型": c["data_type"], "説明": c["description"], "タグ": ", ".join(c["tags"])}
        for c in repo.list_columns(conn, asset_id)
    ],
)
original = pd.DataFrame(col_snap.values)
if can_edit:
    col_snap.stale_notice()
    edited = st.data_editor(
        original,
        key=col_snap.wkey("editor"),
        hide_index=True,
        width="stretch",
        disabled=["id", "カラム", "型"],
        column_order=["カラム", "型", "説明", "タグ"],
        column_config={"タグ": st.column_config.TextColumn(help="カンマ区切りで入力")},
    )
    if st.button("カラム情報を保存", key=col_snap.wkey("save")):
        changed = missing = 0
        for before, after in zip(original.to_dict("records"), edited.to_dict("records")):
            new_desc = after["説明"] or ""
            old_tags, new_tags = repo.parse_tag_text(before["タグ"]), repo.parse_tag_text(after["タグ"])
            if new_desc == before["説明"] and new_tags == old_tags:
                continue
            ok = repo.update_column_metadata(
                conn,
                user,
                int(before["id"]),
                description=new_desc if new_desc != before["説明"] else None,
                add_tags=[t for t in new_tags if t not in old_tags],
                remove_tags=[t for t in old_tags if t not in new_tags],
            )
            changed += ok
            missing += not ok
        note = f"（{missing} カラムはデータ更新で削除されていたため保存できませんでした）" if missing else ""
        saved(f"{changed} カラムを更新しました{note}", col_snap)
else:
    st.dataframe(original, hide_index=True, width="stretch", column_order=["カラム", "型", "説明", "タグ"])

# ---- リネージ
st.subheader("リネージ（直接の上流・下流）")
names = asset_options(conn)
lin_snap = FormSnapshot(
    "lineage",
    asset_id,
    lambda: {
        "up": sorted(repo.direct_upstreams(conn, asset_id)),
        "down": sorted(repo.direct_downstreams(conn, asset_id)),
    },
)
if can_edit:
    lin_snap.stale_notice()
    others = {i: n for i, n in names.items() if i != asset_id}
    with st.form(lin_snap.wkey("form")):
        c1, c2 = st.columns(2)
        ups = c1.multiselect(
            "上流（このアセットの元になるデータ）",
            list(others),
            default=[i for i in lin_snap.values["up"] if i in others],
            format_func=others.get,
            key=lin_snap.wkey("up"),
        )
        downs = c2.multiselect(
            "下流（このアセットを使うデータ）",
            list(others),
            default=[i for i in lin_snap.values["down"] if i in others],
            format_func=others.get,
            key=lin_snap.wkey("down"),
        )
        if st.form_submit_button("リネージを保存", type="primary"):
            if set(ups) & set(downs):
                st.error("同じアセットを上流と下流の両方には指定できません。")
            else:
                before = lin_snap.values
                errors = repo.apply_lineage_changes(
                    conn,
                    user,
                    asset_id,
                    add_up=[i for i in ups if i not in before["up"]],
                    remove_up=[i for i in before["up"] if i not in ups],
                    add_down=[i for i in downs if i not in before["down"]],
                    remove_down=[i for i in before["down"] if i not in downs],
                )
                for e in errors:
                    st.error(e)
                if not errors:
                    saved("リネージを保存しました", lin_snap)
else:
    c1, c2 = st.columns(2)
    c1.markdown("**上流**: " + ("、".join(names.get(i, "?") for i in repo.direct_upstreams(conn, asset_id)) or "なし"))
    c2.markdown("**下流**: " + ("、".join(names.get(i, "?") for i in repo.direct_downstreams(conn, asset_id)) or "なし"))
st.page_link(LINEAGE_PAGE, label="リネージグラフで見る", icon="🔀", query_params={"id": asset_id})

# ---- プレビュー
st.subheader("プレビュー（先頭20行）")
try:
    st.dataframe(preview(asset["file_path"]), width="stretch")
except Exception as e:
    st.warning(f"ファイルを読み込めませんでした: {e}")

# ---- 変更履歴
st.subheader("変更履歴（最新50件）")
history = changelog.list_changes(conn, asset_id=asset_id, limit=50)
if history:
    st.dataframe(changelog.to_display_rows(history, with_asset=False), hide_index=True, width="stretch")
else:
    st.caption("まだ変更履歴はありません。")

# ---- データ更新（パワーユーザー以上）
if can_manage:
    st.subheader("データ更新")
    st.caption(
        "ファイルの中身が変わったときはここから更新します。アセットの説明・タグ・プロパティ・リネージと、"
        "同じ名前（または対応付けた）カラムの説明・タグは引き継がれます。"
    )
    mode = st.radio("更新方法", ["元ファイルから再取込", "ファイルを差し替え"], horizontal=True, key=f"mode_{asset_id}")
    source: Path | None = None
    uploaded = False
    if mode == "元ファイルから再取込":
        current = Path(asset["file_path"])
        if current.is_file():
            source = current
        else:
            st.warning(f"元ファイルが見つかりません: `{current}`。「ファイルを差し替え」で新しいファイルを指定してください。")
    else:
        # 更新後にアップローダーを空に戻すため、キーに世代番号を付ける
        gen_key = f"replace_gen_{asset_id}"
        gen = st.session_state.setdefault(gen_key, 0)
        f = st.file_uploader(
            "新しいファイル", type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS], key=f"replace_{asset_id}_{gen}"
        )
        if f is not None:
            try:
                source, uploaded = stage_upload(f), True
            except ValueError as e:
                st.error(f"{f.name}: {e}")

    if source is not None:
        try:
            new_schema = infer_schema(source)
        except Exception as e:
            st.error(f"{source.name}: 読み込みに失敗しました（{e}）")
        else:
            st.caption(
                f"{new_schema.format} ・ {new_schema.row_count:,} 行（現在 {asset['row_count']:,} 行）・ "
                f"{len(new_schema.columns)} カラム"
            )
            with st.expander("新しいスキーマ"):
                schema_table(new_schema)
            renames, ok = review_changes(conn, asset["name"], new_schema, key=f"reingest_{asset_id}")
            if st.button("更新する", type="primary", disabled=not ok, key=f"reingest_btn_{asset_id}"):
                path = store_upload(source, asset["name"]) if uploaded else source
                try:
                    r = repo.reingest_asset(conn, user, asset_id, str(path), new_schema, renames)
                except (ValueError, PermissionError) as e:
                    if uploaded:
                        discard_upload(path)  # 更新できなかったので、移したファイルを片付ける
                    st.error(str(e))
                    st.stop()
                if uploaded:
                    # DB の更新が済んでから、置き換え前のアップロードファイルを消す
                    discard_upload(asset["file_path"], keep=path)
                parts = [f"{new_schema.row_count:,} 行"]
                if r.renamed_columns:
                    parts.append("引き継ぎ: " + ", ".join(f"{o} → {n}" for o, n in r.renamed_columns.items()))
                if r.added_columns:
                    parts.append("追加: " + ", ".join(r.added_columns))
                if r.removed_columns:
                    parts.append("削除: " + ", ".join(r.removed_columns))
                if uploaded:
                    st.session_state[gen_key] += 1
                saved("データを更新しました（" + " / ".join(parts) + "）", col_snap)

    # ---- 削除
    with st.expander("アセットを削除"):
        st.write("カラム・タグ付け・プロパティ・リネージも一緒に削除されます。元のファイルは削除しません。変更履歴は残ります。")
        if st.checkbox("削除してよいことを確認しました", key=f"del_confirm_{asset_id}"):
            if st.button("削除する", type="secondary"):
                repo.delete_asset(conn, user, asset_id)
                st.query_params.clear()
                st.rerun()
