"""ユーザー・認証・権限。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from enum import IntEnum

from catalog import changelog
from catalog.tx import write_tx

ROLES = {"admin": "管理者", "power": "パワーユーザー", "editor": "編集者", "viewer": "閲覧者"}
_LEVEL = {"viewer": 0, "editor": 1, "power": 2, "admin": 3}
MIN_PASSWORD_LENGTH = 8


class Perm(IntEnum):
    VIEW = 0  # 閲覧
    EDIT = 1  # 説明・タグ・プロパティ値・カラム・リネージの編集
    MANAGE_ASSETS = 2  # アセットの登録・データ更新・削除
    ADMIN = 3  # プロパティ定義・ユーザー管理・全体の変更履歴


PERM_ROLE = {Perm.VIEW: "viewer", Perm.EDIT: "editor", Perm.MANAGE_ASSETS: "power", Perm.ADMIN: "admin"}


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    active: bool = True
    must_change_password: bool = False
    email: str = ""
    description: str = ""

    @property
    def role_label(self) -> str:
        return ROLES[self.role]


def can(user: User | None, perm: Perm) -> bool:
    return user is not None and user.active and _LEVEL[user.role] >= perm


def require(user: User | None, perm: Perm) -> None:
    if not can(user, perm):
        raise PermissionError(f"この操作には「{ROLES[PERM_ROLE[perm]]}」以上の権限が必要です")


# ---------------------------------------------------------------- パスワード

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), digest)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"パスワードは {MIN_PASSWORD_LENGTH} 文字以上にしてください")


# ---------------------------------------------------------------- 参照

def _row_to_user(r: sqlite3.Row) -> User:
    return User(
        r["id"], r["username"], r["display_name"], r["role"], bool(r["active"]), bool(r["must_change_password"]),
        r["email"], r["description"],
    )


def has_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(r) if r else None


def list_users(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, username, display_name, email, description, role, active, must_change_password, "
            "created_at, last_login_at "
            "FROM users ORDER BY active DESC, username"
        )
    ]


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    r = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if r is None:
        verify_password(password, hash_password("dummy-password"))  # ユーザーの有無で応答時間を変えない
        return None
    if not r["active"] or not verify_password(password, r["password_hash"]):
        return None
    conn.execute("UPDATE users SET last_login_at = datetime('now', 'localtime') WHERE id = ?", (r["id"],))
    return _row_to_user(r)


# ---------------------------------------------------------------- 変更

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(conn: sqlite3.Connection, email: str | None, exclude_id: int | None) -> str:
    """空なら ''。形式を確かめ、小文字にそろえ、他のユーザーとの重複を拒む。"""
    email = (email or "").strip().lower()
    if not email:
        return ""
    if not _EMAIL.match(email):
        raise ValueError(f"メールアドレスの形式が正しくありません: {email}")
    if conn.execute("SELECT 1 FROM users WHERE email = ? AND id IS NOT ?", (email, exclude_id)).fetchone():
        raise ValueError(f"メールアドレス「{email}」は他のユーザーが使っています")
    return email

def _validate_username(username: str) -> str:
    username = username.strip()
    if not username:
        raise ValueError("ユーザー ID を入力してください")
    if any(c.isspace() for c in username):
        raise ValueError("ユーザー ID に空白は使えません")
    return username


def _insert_user(conn, username, display_name, role, password, must_change, email="", description="") -> int:
    if role not in ROLES:
        raise ValueError(f"未対応のロールです: {role}")
    _check_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError(f"ユーザー ID「{username}」は既に使われています")
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, role, must_change_password, email, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, display_name.strip() or username, hash_password(password), role, int(must_change),
         _normalize_email(conn, email, None), (description or "").strip()),
    )
    return cur.lastrowid


def create_first_admin(
    conn: sqlite3.Connection, username: str, display_name: str, password: str, email: str = "", description: str = ""
) -> User:
    """ユーザーが1人もいないときだけ、初期管理者を作る。"""
    username = _validate_username(username)
    with write_tx(conn):
        if has_users(conn):
            raise PermissionError("既にユーザーが登録されています")
        uid = _insert_user(conn, username, display_name, "admin", password, False, email, description)
        user = get_user(conn, uid)
        changelog.log(conn, user, "user", "create", target=username, new="管理者（初期セットアップ）")
    return user


def create_user(
    conn: sqlite3.Connection,
    actor: User,
    username: str,
    display_name: str,
    role: str,
    password: str,
    must_change_password: bool = True,
    email: str = "",
    description: str = "",
) -> int:
    require(actor, Perm.ADMIN)
    username = _validate_username(username)
    with write_tx(conn):
        uid = _insert_user(conn, username, display_name, role, password, must_change_password, email, description)
        changelog.log(conn, actor, "user", "create", target=username, new=ROLES[role])
    return uid


def _ensure_admin_remains(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]
    if n == 0:
        raise ValueError("有効な管理者が1人もいなくなるため、この変更はできません")


def update_user(
    conn: sqlite3.Connection,
    actor: User,
    user_id: int,
    role: str | None = None,
    display_name: str | None = None,
    active: bool | None = None,
    email: str | None = None,
    description: str | None = None,
) -> None:
    """渡された項目だけを変更する（None は変更しない）。"""
    require(actor, Perm.ADMIN)
    with write_tx(conn):
        cur = get_user(conn, user_id)
        if cur is None:
            raise ValueError("ユーザーが存在しません")
        _update_profile(conn, actor, cur, email, description)
        if role is not None and role != cur.role:
            if role not in ROLES:
                raise ValueError(f"未対応のロールです: {role}")
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            changelog.log(conn, actor, "user", "update", target=f"{cur.username} / ロール", old=ROLES[cur.role], new=ROLES[role])
        if display_name is not None and display_name.strip() and display_name.strip() != cur.display_name:
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name.strip(), user_id))
            changelog.log(conn, actor, "user", "update", target=f"{cur.username} / 表示名", old=cur.display_name, new=display_name.strip())
        if active is not None and active != cur.active:
            conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
            changelog.log(conn, actor, "user", "update", target=f"{cur.username} / 状態", old="有効" if cur.active else "無効", new="有効" if active else "無効")
        _ensure_admin_remains(conn)  # 違反ならトランザクションごと取り消す


def _update_profile(conn, actor: User, cur: User, email: str | None, description: str | None) -> None:
    """メールアドレス・説明の変更（呼び出し側の write_tx の中で使う）。"""
    if email is not None:
        new = _normalize_email(conn, email, cur.id)
        if new != cur.email:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (new, cur.id))
            changelog.log(conn, actor, "user", "update", target=f"{cur.username} / メールアドレス", old=cur.email, new=new)
    if description is not None and description.strip() != cur.description:
        conn.execute("UPDATE users SET description = ? WHERE id = ?", (description.strip(), cur.id))
        changelog.log(conn, actor, "user", "update", target=f"{cur.username} / 説明",
                      old=cur.description, new=description.strip())


def update_own_profile(
    conn: sqlite3.Connection, user: User, email: str | None = None, description: str | None = None
) -> None:
    """本人によるメールアドレス・説明の変更（ロールや状態は変えられない）。"""
    with write_tx(conn):
        cur = get_user(conn, user.id)
        if cur is None or not cur.active:
            raise PermissionError("このアカウントは無効です")
        _update_profile(conn, user, cur, email, description)


def reset_password(conn: sqlite3.Connection, actor: User, user_id: int, new_password: str) -> None:
    """管理者によるパスワードリセット。次回ログイン時に変更を求める。"""
    require(actor, Perm.ADMIN)
    _check_password(new_password)
    with write_tx(conn):
        cur = get_user(conn, user_id)
        if cur is None:
            raise ValueError("ユーザーが存在しません")
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        changelog.log(conn, actor, "user", "update", target=f"{cur.username} / パスワード", new="リセット")


def change_own_password(conn: sqlite3.Connection, user: User, old_password: str, new_password: str) -> None:
    _check_password(new_password)
    if old_password == new_password:
        raise ValueError("新しいパスワードは現在のものと別にしてください")
    with write_tx(conn):
        r = conn.execute("SELECT password_hash, active FROM users WHERE id = ?", (user.id,)).fetchone()
        if r is None or not r["active"] or not verify_password(old_password, r["password_hash"]):
            raise ValueError("現在のパスワードが違います")
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new_password), user.id),
        )
        changelog.log(conn, user, "user", "update", target=f"{user.username} / パスワード", new="本人が変更")
