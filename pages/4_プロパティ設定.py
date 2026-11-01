"""プロパティ定義の管理（標準プロパティの調整とカスタムプロパティの追加・変更・削除）。"""

import streamlit as st

from catalog import properties as props
from catalog.auth import Perm
from catalog.ui import require_page

st.title("⚙️ プロパティ設定")
st.caption(
    "アセットに付けるプロパティを管理します。🔒 の標準プロパティは削除や型の変更はできませんが、"
    "名前・選択肢・必須・説明は変更できます。必須にした項目は、各アセットの充実度に数えられます。"
)

conn, user = require_page(Perm.ADMIN)
TYPE_KEYS = list(props.TYPES)

if msg := st.session_state.pop("prop_settings_msg", None):
    st.success(msg)


def parse_choices(text: str) -> list[str]:
    return [c.strip() for c in text.replace("、", ",").split(",") if c.strip()]


# ---- 一覧
defs = props.list_definitions(conn)
st.subheader("定義一覧")
st.dataframe(
    [
        {
            "": "🔒" if d.builtin else "",
            "名前": d.name,
            "型": props.TYPES[d.type],
            "必須": "＊" if d.required else "",
            "選択肢": " / ".join(d.choices),
            "入力済みアセット数": props.value_count(conn, d.id),
            "説明": d.help,
        }
        for d in defs
    ],
    hide_index=True,
    width="stretch",
)

# ---- カスタムプロパティの追加
st.subheader("カスタムプロパティを追加")
with st.form("add_property", clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    name = c1.text_input("名前", placeholder="例: コスト負担部署")
    type_ = c2.selectbox("型", TYPE_KEYS, format_func=props.TYPES.get)
    required = c3.checkbox("必須", help="必須にすると、各アセットの充実度に数えられます")
    choices = st.text_input("選択肢（型が「選択肢」のとき。カンマ区切り）", placeholder="例: A, B, C")
    help_ = st.text_input("説明（入力欄のヘルプに表示されます）")
    if st.form_submit_button("追加", type="primary"):
        try:
            props.create_definition(conn, user, name, type_, parse_choices(choices), required, help_)
        except ValueError as e:
            st.error(str(e))
        else:
            st.session_state["prop_settings_msg"] = f"「{name.strip()}」を追加しました"
            st.rerun()

# ---- 編集・削除
st.subheader("編集")
for d in defs:
    count = props.value_count(conn, d.id)
    with st.expander(f"{'🔒 ' if d.builtin else ''}{d.name}（{props.TYPES[d.type]}{'・必須' if d.required else ''}）"):
        with st.form(f"edit_{d.id}"):
            c1, c2, c3 = st.columns([2, 1, 1])
            new_name = c1.text_input("名前", value=d.name)
            # 標準プロパティ、または値が入っているカスタムプロパティは型を変えられない
            type_locked = d.builtin or count > 0
            new_type = c2.selectbox(
                "型",
                TYPE_KEYS,
                index=TYPE_KEYS.index(d.type),
                format_func=props.TYPES.get,
                disabled=type_locked,
                help="標準プロパティ、または値が入力済みのプロパティは型を変更できません" if type_locked else None,
            )
            new_required = c3.checkbox("必須", value=d.required)
            new_choices = st.text_input("選択肢（カンマ区切り）", value=", ".join(d.choices))
            new_help = st.text_input("説明", value=d.help)
            if st.form_submit_button("保存"):
                try:
                    props.update_definition(
                        conn, user, d.id, new_name, new_type, parse_choices(new_choices), new_required, new_help
                    )
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.session_state["prop_settings_msg"] = f"「{new_name.strip()}」を更新しました"
                    st.rerun()
        if d.type == "select" and count:
            st.caption("選択肢から外した値も、入力済みのアセットにはそのまま残ります。")
        if not d.builtin:
            confirm = st.checkbox(
                f"削除する（{count} 件のアセットに入力された値も消えます）", key=f"del_confirm_{d.id}"
            )
            if st.button("削除", key=f"del_{d.id}", disabled=not confirm):
                props.delete_definition(conn, user, d.id)
                st.session_state["prop_settings_msg"] = f"「{d.name}」を削除しました"
                st.rerun()
