import sqlite3

import pytest

from catalog import auth
from catalog.db import SCHEMA_VERSION, init_db, open_db


def test_outdated_db_is_backed_up_and_recreated(tmp_path):
    path = tmp_path / "catalog.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name TEXT)")
    old.execute("INSERT INTO assets (name) VALUES ('old')")
    old.commit()
    old.close()

    backup = init_db(path)
    assert backup is not None and backup.exists() and backup.name.startswith("catalog.db.bak-")
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert sqlite3.connect(backup).execute("SELECT name FROM assets").fetchone()[0] == "old"


def test_current_db_is_kept(tmp_path):
    path = tmp_path / "catalog.db"
    conn = open_db(path)
    auth.create_first_admin(conn, "admin", "", "admin-password")
    conn.close()
    assert init_db(path) is None
    assert auth.has_users(open_db(path))


def test_v2_db_is_upgraded_in_place(tmp_path):
    from catalog import repository as repo
    from catalog.ingest import ColumnSchema, FileSchema

    path = tmp_path / "catalog.db"
    conn = open_db(path)
    admin = auth.create_first_admin(conn, "admin", "", "admin-password")
    repo.register_asset(conn, admin, "a", "a.csv", FileSchema("csv", 1, [ColumnSchema("x", "int64")]))
    conn.execute("DROP TABLE question_assets")
    conn.execute("DROP TABLE questions")
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    assert init_db(path) is None  # 退避しない
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT name FROM assets").fetchone()[0] == "a"
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0


def test_v3_db_gets_user_columns(tmp_path):
    """v3（メールアドレス・説明の列がない）DB を、データを残したまま v4 に上げる。"""
    path = tmp_path / "catalog.db"
    conn = open_db(path)
    auth.create_first_admin(conn, "admin", "管理者", "admin-password")
    # v3 相当に戻す: users を email / description なしで作り直す
    conn.executescript("""
        DROP INDEX idx_users_email;
        CREATE TABLE users_old AS SELECT id, username, display_name, password_hash, role, active,
            must_change_password, created_at, last_login_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_old RENAME TO users;
        PRAGMA user_version = 3;
    """)
    conn.close()

    assert init_db(path) is None
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    u = auth.authenticate(conn, "admin", "admin-password")
    assert (u.display_name, u.email, u.description) == ("管理者", "", "")


def test_v4_db_gets_asset_status(tmp_path):
    """v4（ステータスの列がない）DB を、データを残したまま v5 に上げる。既存のアセットは運用中になる。"""
    from catalog import repository as repo
    from catalog.ingest import ColumnSchema, FileSchema

    path = tmp_path / "catalog.db"
    conn = open_db(path)
    admin = auth.create_first_admin(conn, "admin", "", "admin-password")
    repo.register_asset(conn, admin, "a", "a.csv", FileSchema("csv", 1, [ColumnSchema("x", "int64")]))
    conn.execute("ALTER TABLE assets DROP COLUMN status")
    conn.execute("PRAGMA user_version = 4")
    conn.close()

    assert init_db(path) is None
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert tuple(conn.execute("SELECT name, status FROM assets").fetchone()) == ("a", "運用中")
    with pytest.raises(sqlite3.IntegrityError):  # 移行後も CHECK 制約が効く
        conn.execute("UPDATE assets SET status = '不明'")


def test_v5_db_gets_metrics(tmp_path):
    """v5（指標・ビジネスプロセスがない）DB を、データを残したまま v6 に上げる。"""
    from catalog import repository as repo
    from catalog.ingest import ColumnSchema, FileSchema

    path = tmp_path / "catalog.db"
    conn = open_db(path)
    admin = auth.create_first_admin(conn, "admin", "", "admin-password")
    repo.register_asset(conn, admin, "a", "a.csv", FileSchema("csv", 1, [ColumnSchema("x", "int64")]))
    for table in ("metric_questions", "metric_assets", "metrics", "business_processes"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    assert init_db(path) is None  # 退避しない
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT name FROM assets").fetchone()[0] == "a"
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_processes").fetchone()[0] == 0
