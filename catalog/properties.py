"""アセットプロパティ（標準＋カスタム）の定義・値・充実度。

標準プロパティとカスタムプロパティは同じ property_definitions テーブルに置き、
builtin_key の有無で区別する。標準プロパティは削除・型変更ができない。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from catalog import changelog, roles
from catalog.auth import Perm, User, require
from catalog.tx import write_tx

TYPES = {
    "text": "テキスト",
    "longtext": "長文テキスト",
    "number": "数値",
    "date": "日付",
    "select": "選択肢",
    "url": "URL",
}

# (builtin_key, 名前, 型, 必須, 選択肢, 説明)
# オーナー・スチュワードはプロパティではなく役割割り当て（catalog/roles.py）で持つ
BUILTIN_PROPERTIES: list[tuple[str, str, str, bool, list[str], str]] = [
    ("source_system", "ソースシステム", "text", True, [], "データの発生元・取得元のシステム"),
    (
        "update_frequency",
        "更新頻度",
        "select",
        True,
        ["リアルタイム", "毎時", "日次", "週次", "月次", "四半期", "年次", "不定期", "更新なし"],
        "",
    ),
    ("visibility", "公開範囲", "select", True, ["全社公開", "部門内", "チーム内", "関係者限定"], "誰が閲覧・利用できるか"),
    ("sensitivity", "機密区分", "select", True, ["公開", "社内限定", "機密", "機密（個人情報を含む）"], ""),
    ("retention", "保持期間", "text", True, [], "例: 5年、無期限"),
    ("usage_notes", "利用上の注意", "longtext", False, [], "利用条件、既知の品質問題など"),
]

DESCRIPTION_LABEL = "説明"  # 充実度ではアセットの説明も必須項目として数える
OWNER_LABEL = "オーナー"  # 役割割り当て（catalog/roles.py）も充実度に数える
STEWARD_LABEL = "スチュワード"


@dataclass
class PropertyDef:
    id: int
    name: str
    type: str
    choices: list[str] = field(default_factory=list)
    required: bool = False
    builtin_key: str | None = None
    ordinal: int = 0
    help: str = ""

    @property
    def builtin(self) -> bool:
        return self.builtin_key is not None


def _row_to_def(r: sqlite3.Row) -> PropertyDef:
    return PropertyDef(
        id=r["id"],
        name=r["name"],
        type=r["type"],
        choices=json.loads(r["choices"] or "[]"),
        required=bool(r["required"]),
        builtin_key=r["builtin_key"],
        ordinal=r["ordinal"],
        help=r["help"],
    )


# ---------------------------------------------------------------- 定義

def seed_builtin(conn: sqlite3.Connection) -> None:
    """標準プロパティを登録する。既にあるもの（ユーザーが変更したものを含む）は上書きしない。"""
    with write_tx(conn):
        for ordinal, (key, name, type_, required, choices, help_) in enumerate(BUILTIN_PROPERTIES):
            if conn.execute("SELECT 1 FROM property_definitions WHERE builtin_key = ?", (key,)).fetchone():
                continue
            # 同名のカスタムプロパティがあれば名前を避ける
            final = name
            while conn.execute("SELECT 1 FROM property_definitions WHERE name = ?", (final,)).fetchone():
                final += "（標準）"
            conn.execute(
                "INSERT INTO property_definitions (name, type, choices, required, builtin_key, ordinal, help) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (final, type_, json.dumps(choices, ensure_ascii=False), int(required), key, ordinal, help_),
            )


def list_definitions(conn: sqlite3.Connection) -> list[PropertyDef]:
    rows = conn.execute(
        "SELECT * FROM property_definitions ORDER BY builtin_key IS NULL, ordinal, id"
    ).fetchall()
    return [_row_to_def(r) for r in rows]


def get_definition(conn: sqlite3.Connection, def_id: int) -> PropertyDef:
    r = conn.execute("SELECT * FROM property_definitions WHERE id = ?", (def_id,)).fetchone()
    if r is None:
        raise ValueError(f"プロパティ {def_id} は存在しません")
    return _row_to_def(r)


def get_builtin(conn: sqlite3.Connection, key: str) -> PropertyDef:
    r = conn.execute("SELECT * FROM property_definitions WHERE builtin_key = ?", (key,)).fetchone()
    if r is None:
        raise ValueError(f"標準プロパティ {key} は存在しません")
    return _row_to_def(r)


def _clean_choices(choices) -> list[str]:
    out: list[str] = []
    for c in choices or []:
        c = str(c).strip()
        if c and c not in out:
            out.append(c)
    return out


def _validate_def(conn: sqlite3.Connection, name: str, type_: str, choices: list[str], exclude_id: int | None) -> None:
    if not name:
        raise ValueError("プロパティ名を入力してください")
    if name in (DESCRIPTION_LABEL, OWNER_LABEL, STEWARD_LABEL):
        raise ValueError(f"「{name}」は充実度の項目と紛らわしいため使えません")
    if type_ not in TYPES:
        raise ValueError(f"未対応の型です: {type_}")
    if type_ == "select" and not choices:
        raise ValueError("選択肢型には選択肢を1つ以上指定してください")
    dup = conn.execute(
        "SELECT id FROM property_definitions WHERE name = ? AND id IS NOT ?", (name, exclude_id)
    ).fetchone()
    if dup:
        raise ValueError(f"プロパティ名「{name}」は既に使われています")


def _def_summary(name: str, type_: str, choices: list[str], required: bool, help_: str) -> str:
    parts = [f"名前={name}", f"型={TYPES[type_]}", f"必須={'はい' if required else 'いいえ'}"]
    if choices:
        parts.append("選択肢=" + "/".join(choices))
    if help_:
        parts.append(f"説明={help_}")
    return " ; ".join(parts)


def create_definition(
    conn: sqlite3.Connection,
    actor: User,
    name: str,
    type_: str,
    choices=(),
    required: bool = False,
    help: str = "",
) -> int:
    require(actor, Perm.ADMIN)
    name = name.strip()
    choices = _clean_choices(choices) if type_ == "select" else []
    with write_tx(conn):
        _validate_def(conn, name, type_, choices, None)
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM property_definitions WHERE builtin_key IS NULL"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO property_definitions (name, type, choices, required, ordinal, help) VALUES (?, ?, ?, ?, ?, ?)",
            (name, type_, json.dumps(choices, ensure_ascii=False), int(required), ordinal, help.strip()),
        )
        changelog.log(conn, actor, "definition", "create", target=name,
                      new=_def_summary(name, type_, choices, required, help.strip()))
    return cur.lastrowid


def value_count(conn: sqlite3.Connection, def_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM asset_properties WHERE property_id = ?", (def_id,)).fetchone()[0]


def update_definition(
    conn: sqlite3.Connection,
    actor: User,
    def_id: int,
    name: str,
    type_: str,
    choices=(),
    required: bool = False,
    help: str = "",
) -> None:
    require(actor, Perm.ADMIN)
    name = name.strip()
    choices = _clean_choices(choices) if type_ == "select" else []
    with write_tx(conn):
        current = get_definition(conn, def_id)
        if type_ != current.type:
            if current.builtin:
                raise ValueError("標準プロパティの型は変更できません")
            if value_count(conn, def_id):
                raise ValueError("値が入力されているアセットがあるため、型は変更できません")
        _validate_def(conn, name, type_, choices, def_id)
        conn.execute(
            "UPDATE property_definitions SET name = ?, type = ?, choices = ?, required = ?, help = ? WHERE id = ?",
            (name, type_, json.dumps(choices, ensure_ascii=False), int(required), help.strip(), def_id),
        )
        old = _def_summary(current.name, current.type, current.choices, current.required, current.help)
        new = _def_summary(name, type_, choices, required, help.strip())
        if old != new:
            changelog.log(conn, actor, "definition", "update", target=current.name, old=old, new=new)


def delete_definition(conn: sqlite3.Connection, actor: User, def_id: int) -> None:
    require(actor, Perm.ADMIN)
    with write_tx(conn):
        current = get_definition(conn, def_id)
        if current.builtin:
            raise ValueError("標準プロパティは削除できません")
        n = value_count(conn, def_id)
        conn.execute("DELETE FROM property_definitions WHERE id = ?", (def_id,))
        changelog.log(conn, actor, "definition", "delete", target=current.name,
                      old=_def_summary(current.name, current.type, current.choices, current.required, current.help),
                      new=f"入力済みの値 {n} 件も削除")


# ---------------------------------------------------------------- 値

_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


MAX_NUMBER_DIGITS = 60  # 保存する数値の桁数の上限（整数部＋小数部）


def _normalize_number(name: str, raw: str, v: str) -> str:
    """数値を Decimal で正確に正規化する（float を通すと 2**53 を超える整数や長い小数の精度が落ちるため）。

    指数表記は展開し、小数部の末尾の 0 は取り除く（"1.50" → "1.5"、"1e3" → "1000"）。
    有限でない値と、展開すると MAX_NUMBER_DIGITS 桁を超える値は拒否する。
    """
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{name}: 数値を入力してください（入力: {raw}）") from None
    if not d.is_finite():
        raise ValueError(f"{name}: 有限の数値を入力してください（入力: {raw}）")
    sign, digits, exp = d.as_tuple()
    int_digits = max(len(digits) + exp, 1)
    frac_digits = max(-exp, 0)
    if int_digits + frac_digits > MAX_NUMBER_DIGITS:
        raise ValueError(f"{name}: 桁数が多すぎます（{MAX_NUMBER_DIGITS} 桁まで。入力: {raw}）")
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", "+0", ""):
        text = "0"
    return text


def normalize_value(defn: PropertyDef, raw) -> str | None:
    """型に合わせて値を検証・正規化する。空なら None。不正なら ValueError。"""
    if raw is None:
        return None
    if isinstance(raw, date):
        raw = raw.isoformat()
    value = str(raw).strip()
    if not value:
        return None
    t = defn.type
    if t == "number":
        v = value.replace(",", "")
        if not _NUMBER.match(v):
            raise ValueError(f"{defn.name}: 数値を入力してください（入力: {value}）")
        return _normalize_number(defn.name, value, v)
    if t == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise ValueError(f"{defn.name}: 日付は YYYY-MM-DD 形式で入力してください（入力: {value}）") from None
    if t == "select" and value not in defn.choices:
        raise ValueError(f"{defn.name}: 選択肢にない値です（入力: {value}）")
    if t == "url" and not re.match(r"^https?://\S+$", value):
        raise ValueError(f"{defn.name}: http:// または https:// で始まる URL を入力してください")
    return value


def get_asset_properties(conn: sqlite3.Connection, asset_id: int) -> dict[int, str]:
    return {
        r[0]: r[1]
        for r in conn.execute("SELECT property_id, value FROM asset_properties WHERE asset_id = ?", (asset_id,))
    }


def set_asset_properties(
    conn: sqlite3.Connection, actor: User, asset_id: int, values: dict[int, object]
) -> list[str]:
    """渡された項目だけを保存する（項目単位のあと勝ち）。

    1つでも不正な値があれば何も保存せず、エラー一覧を返す。空の値は削除として扱う。
    画面からは「開いた時点から変えた項目」だけを渡すこと。
    """
    require(actor, Perm.EDIT)
    with write_tx(conn):
        defs = {d.id: d for d in list_definitions(conn)}
        current = get_asset_properties(conn, asset_id)
        normalized: dict[int, str | None] = {}
        errors: list[str] = []
        for def_id, raw in values.items():
            if def_id not in defs:
                errors.append(f"プロパティ {def_id} は存在しません")
                continue
            if raw is not None and str(raw).strip() == current.get(def_id):
                # 変更していない値は検証しない（選択肢から外された古い値でも他の項目を保存できるように）
                continue
            try:
                normalized[def_id] = normalize_value(defs[def_id], raw)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            return errors
        changed = False
        for def_id, v in normalized.items():
            old = current.get(def_id)
            if v == old:
                continue
            changed = True
            if v is None:
                conn.execute(
                    "DELETE FROM asset_properties WHERE asset_id = ? AND property_id = ?", (asset_id, def_id)
                )
            else:
                conn.execute(
                    "INSERT INTO asset_properties (asset_id, property_id, value) VALUES (?, ?, ?) "
                    "ON CONFLICT (asset_id, property_id) DO UPDATE SET value = excluded.value",
                    (asset_id, def_id, v),
                )
            changelog.log(conn, actor, "property", "update", asset_id=asset_id, target=defs[def_id].name, old=old, new=v)
        if changed:
            conn.execute("UPDATE assets SET updated_at = datetime('now', 'localtime') WHERE id = ?", (asset_id,))
    return []


def distinct_values(conn: sqlite3.Connection, def_id: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT value FROM asset_properties WHERE property_id = ? ORDER BY value", (def_id,)
        )
    ]


# ---------------------------------------------------------------- 充実度

@dataclass
class Completeness:
    filled: int
    total: int
    missing: list[str]

    @property
    def complete(self) -> bool:
        return self.filled == self.total


def completeness(conn: sqlite3.Connection, asset_id: int, defs: list[PropertyDef] | None = None) -> Completeness:
    """必須項目のうち入力済みの数。

    対象: アセットの説明、必須プロパティ、オーナー（すべての責任範囲が誰かに割り当てられていること）、
    スチュワード（1人以上）。オーナー・スチュワードはドメインからの継承を含めて判定する。
    """
    defs = defs if defs is not None else list_definitions(conn)
    values = get_asset_properties(conn, asset_id)
    row = conn.execute("SELECT description FROM assets WHERE id = ?", (asset_id,)).fetchone()
    missing = [] if row and row["description"].strip() else [DESCRIPTION_LABEL]
    required = [d for d in defs if d.required]
    missing += [d.name for d in required if not values.get(d.id)]
    effective = roles.effective_assignments(conn, asset_id)
    uncovered = roles.uncovered_owner_scopes(effective)
    if uncovered:
        missing.append(OWNER_LABEL if len(uncovered) == len(roles.OWNER_SCOPES)
                       else f"{OWNER_LABEL}（{'・'.join(uncovered)}）")
    if not roles.has_steward(effective):
        missing.append(STEWARD_LABEL)
    total = len(required) + 3
    return Completeness(total - len(missing), total, missing)
