"""データファイルの登録（新規・再取込）。"""

from pathlib import Path

import streamlit as st

from catalog import repository as repo
from catalog import roles
from catalog.ingest import SUPPORTED_EXTENSIONS, detect_format, infer_schema
from catalog.auth import Perm
from catalog.ui import (
    DETAIL_PAGE,
    QUESTIONS_PAGE,
    discard_upload,
    require_page,
    review_changes,
    schema_table,
    stage_upload,
    store_upload,
)

st.title("➕ データ登録")
st.caption(f"対応形式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}。既存アセットのデータ更新は、アセット詳細ページの「データ更新」からも行えます。")

conn, user = require_page(Perm.MANAGE_ASSETS)

# ---- 候補ファイルを集める: (アセット名, 読み込むパス, アップロードかどうか)
candidates: list[tuple[str, Path, bool]] = []

tab_upload, tab_local = st.tabs(["ファイルをアップロード", "ローカルパスを指定"])
with tab_upload:
    files = st.file_uploader(
        "ファイルを選択", type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS], accept_multiple_files=True
    )
    for f in files or []:
        try:
            candidates.append((Path(f.name).stem, stage_upload(f), True))
        except ValueError as e:
            st.error(f"{f.name}: {e}")
with tab_local:
    st.caption("ファイルのパスを参照として登録します（ファイルはコピーしません）。ディレクトリを指定すると直下の対応ファイルをすべて対象にします。")
    local = st.text_input("ファイルまたはディレクトリのパス", placeholder="例: sample_data")
    if local.strip():
        p = Path(local.strip()).expanduser().resolve()
        if p.is_dir():
            found = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() in SUPPORTED_EXTENSIONS)
            if not found:
                st.warning("対応形式のファイルが見つかりません。")
            candidates += [(x.stem, x, False) for x in found]
        elif p.is_file():
            candidates.append((p.stem, p, False))
        else:
            st.error(f"パスが見つかりません: {p}")

if not candidates:
    st.stop()

# ---- 各候補のスキーマと差分を表示
st.subheader("取込内容の確認")
domain_names = {d["id"]: d["name"] for d in roles.list_domains(conn)}
domain_id = st.selectbox(
    "ドメイン（新規登録するアセットに設定。オーナー・スチュワードを継承します）",
    [0, *domain_names],  # 0 = なし
    format_func=lambda i: domain_names.get(i, "（なし）"),
) or None
plans = []
all_ok = True
for i, (default_name, path, uploaded) in enumerate(candidates):
    with st.container(border=True):
        key = f"cand_{i}_{path}"
        name = st.text_input("アセット名", value=default_name, key=f"name_{key}").strip()
        try:
            detect_format(path)
            schema = infer_schema(path)
        except Exception as e:  # 読み込み失敗はその候補だけスキップする
            st.error(f"{path.name}: 読み込みに失敗しました（{e}）")
            continue
        if not name:
            st.error("アセット名を入力してください。")
            continue
        existing = repo.get_asset_by_name(conn, name)
        if existing is None:
            st.markdown(f"**{path.name}** → `{name}`（新規）")
        else:
            st.markdown(
                f"**{path.name}** → 既存アセット `{name}` を **更新** します"
                "（アセットの説明・タグ・リネージはそのまま残ります）"
            )
        st.caption(f"{schema.format} ・ {schema.row_count:,} 行 ・ {len(schema.columns)} カラム")
        schema_table(schema)
        renames, ok = review_changes(conn, name, schema, key)
        all_ok &= ok
        plans.append((name, path, uploaded, schema, renames, existing))

if st.button("登録する", type="primary", disabled=not plans or not all_ok):
    names = [p[0] for p in plans]
    if len(set(names)) != len(names):
        st.error("同じアセット名が複数あります。名前を変えてください。")
        st.stop()
    registered_any = False
    for name, path, uploaded, schema, renames, existing in plans:
        if uploaded:
            path = store_upload(path, name)
        try:
            result = repo.register_asset(conn, user, name, str(path), schema, renames, domain_id=domain_id)
        except (ValueError, PermissionError) as e:
            if uploaded:
                discard_upload(conn, path)  # 登録できなかったので、移したファイルを片付ける
            st.error(f"{name}: {e}")
            continue
        if uploaded and existing:
            # DB の更新が済んでから、置き換え前のアップロードファイルを消す
            discard_upload(conn, existing["file_path"], keep=path)
        verb = "登録" if result.created else "更新"
        st.success(f"{name} を{verb}しました。")
        st.page_link(DETAIL_PAGE, label=f"{name} の詳細を開く", icon="📝", query_params={"id": result.asset_id})
        registered_any = True
    if registered_any:
        st.info("このデータで答えたい問いがあれば、書いておきましょう（あとからでも書けます）。"
                "問いがあると、使う人と守る人がつながり、必要な品質も決めやすくなります。")
        st.page_link(QUESTIONS_PAGE, label="問いを書く", icon="❓")
