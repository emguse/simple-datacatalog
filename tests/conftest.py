import pytest

from catalog import auth
from catalog.db import open_db


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "catalog.db"


@pytest.fixture
def conn(db_path):
    c = open_db(db_path)
    yield c
    c.close()


@pytest.fixture
def admin(conn):
    return auth.create_first_admin(conn, "admin", "管理者", "admin-password")


def _make(conn, admin, role):
    uid = auth.create_user(conn, admin, role, role, role, f"{role}-password", must_change_password=False)
    return auth.get_user(conn, uid)


@pytest.fixture
def power(conn, admin):
    return _make(conn, admin, "power")


@pytest.fixture
def editor(conn, admin):
    return _make(conn, admin, "editor")


@pytest.fixture
def viewer(conn, admin):
    return _make(conn, admin, "viewer")
