import pytest

from catalog import auth
from catalog import properties as props
from catalog import repository as repo
from catalog.auth import Perm
from catalog.ingest import ColumnSchema, FileSchema

S = FileSchema("csv", 1, [ColumnSchema("x", "int64")])


def test_password_hash():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "broken")
    assert auth.hash_password("same") != auth.hash_password("same")  # ソルトが毎回違う


def test_bootstrap_only_once(conn):
    assert not auth.has_users(conn)
    a = auth.create_first_admin(conn, "root", "", "root-password")
    assert a.role == "admin" and a.display_name == "root"
    with pytest.raises(PermissionError):
        auth.create_first_admin(conn, "root2", "", "root-password")


def test_authenticate(conn, admin, editor):
    assert auth.authenticate(conn, "editor", "editor-password") == editor
    assert auth.authenticate(conn, "editor", "nope") is None
    assert auth.authenticate(conn, "ghost", "editor-password") is None
    auth.update_user(conn, admin, editor.id, active=False)
    assert auth.authenticate(conn, "editor", "editor-password") is None


def test_user_validation(conn, admin):
    with pytest.raises(ValueError):
        auth.create_user(conn, admin, "short", "", "editor", "1234567")
    with pytest.raises(ValueError):
        auth.create_user(conn, admin, "has space", "", "editor", "password1")
    with pytest.raises(ValueError):
        auth.create_user(conn, admin, "admin", "", "editor", "password1")
    with pytest.raises(ValueError):
        auth.create_user(conn, admin, "x", "", "superuser", "password1")


def test_last_admin_protected(conn, admin, power):
    with pytest.raises(ValueError):
        auth.update_user(conn, admin, admin.id, role="editor")
    with pytest.raises(ValueError):
        auth.update_user(conn, admin, admin.id, active=False)
    assert auth.get_user(conn, admin.id).role == "admin"  # ロールバックされている
    auth.update_user(conn, admin, power.id, role="admin")
    auth.update_user(conn, admin, admin.id, role="editor")  # 他に管理者がいれば OK
    assert auth.get_user(conn, admin.id).role == "editor"


def test_password_change_and_reset(conn, admin, editor):
    auth.reset_password(conn, admin, editor.id, "temporary-pw")
    u = auth.authenticate(conn, "editor", "temporary-pw")
    assert u.must_change_password
    with pytest.raises(ValueError):
        auth.change_own_password(conn, u, "wrong-old", "brand-new-pw")
    with pytest.raises(ValueError):
        auth.change_own_password(conn, u, "temporary-pw", "short")
    auth.change_own_password(conn, u, "temporary-pw", "brand-new-pw")
    assert not auth.authenticate(conn, "editor", "brand-new-pw").must_change_password


def test_permissions(conn, admin, power, editor, viewer):
    assert [auth.can(u, Perm.EDIT) for u in (admin, power, editor, viewer)] == [True, True, True, False]
    assert [auth.can(u, Perm.MANAGE_ASSETS) for u in (admin, power, editor, viewer)] == [True, True, False, False]
    assert [auth.can(u, Perm.ADMIN) for u in (admin, power, editor, viewer)] == [True, False, False, False]
    assert not auth.can(None, Perm.VIEW)

    a = repo.register_asset(conn, power, "a", "a.csv", S).asset_id
    with pytest.raises(PermissionError):
        repo.register_asset(conn, editor, "b", "b.csv", S)
    with pytest.raises(PermissionError):
        repo.delete_asset(conn, editor, a)
    repo.update_asset_metadata(conn, editor, a, "編集者が書いた説明")
    with pytest.raises(PermissionError):
        repo.update_asset_metadata(conn, viewer, a, "x")
    with pytest.raises(PermissionError):
        props.set_asset_properties(conn, viewer, a, {})
    with pytest.raises(PermissionError):
        repo.apply_lineage_changes(conn, viewer, a)
    with pytest.raises(PermissionError):
        props.create_definition(conn, power, "x", "text")
    with pytest.raises(PermissionError):
        auth.create_user(conn, power, "x", "", "viewer", "password1")

    inactive = auth.User(editor.id, editor.username, "", "admin", active=False)
    assert not auth.can(inactive, Perm.VIEW)


def test_email_and_description(conn, admin, editor, viewer):
    uid = auth.create_user(conn, admin, "yamada", "山田", "editor", "password1",
                           email=" Yamada@Example.com ", description="営業管理課。受注データの窓口")
    u = auth.get_user(conn, uid)
    assert (u.email, u.description) == ("yamada@example.com", "営業管理課。受注データの窓口")
    for bad in ("not-an-email", "a@b", "a b@example.com"):
        with pytest.raises(ValueError, match="形式"):
            auth.create_user(conn, admin, "x1", "", "viewer", "password1", email=bad)
    with pytest.raises(ValueError, match="他のユーザー"):  # 大文字小文字の違いも重複
        auth.create_user(conn, admin, "x2", "", "viewer", "password1", email="YAMADA@example.com")
    auth.create_user(conn, admin, "x3", "", "viewer", "password1")  # 空は何人いてもよい
    auth.create_user(conn, admin, "x4", "", "viewer", "password1", email="")

    auth.update_user(conn, admin, uid, description="異動: 経理部")
    assert auth.get_user(conn, uid).email == "yamada@example.com"  # 渡していない項目は変えない
    auth.update_user(conn, admin, uid, email="")
    assert auth.get_user(conn, uid).email == ""

    auth.update_own_profile(conn, viewer, email="viewer@example.com", description="閲覧のみ")
    v = auth.get_user(conn, viewer.id)
    assert (v.email, v.description, v.role) == ("viewer@example.com", "閲覧のみ", "viewer")
    with pytest.raises(ValueError):
        auth.update_own_profile(conn, editor, email="viewer@example.com")
    with pytest.raises(PermissionError):
        auth.update_user(conn, editor, viewer.id, description="他人は変えられない")

    from catalog import changelog
    targets = [c["target"] for c in changelog.list_changes(conn) if c["entity"] == "user" and c["action"] == "update"]
    assert "viewer / メールアドレス" in targets and "yamada / 説明" in targets
    assert {"email", "description"} <= set(auth.list_users(conn)[0])
