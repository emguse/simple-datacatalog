"""SQLite 接続とスキーマ定義。"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from catalog.properties import seed_builtin

# 環境変数 CATALOG_DB で保存先を変更できる（アップロードファイルは同じディレクトリの uploads/ に置く）
DEFAULT_DB_PATH = Path(
    os.environ.get("CATALOG_DB", Path(__file__).resolve().parent.parent / "data" / "catalog.db")
).resolve()

# スキーマの版。
# - UPGRADABLE_FROM の版: その場で上げる（データは残る）。MIGRATIONS の手順を古い順に流してから SCHEMA を実行する
# - それ以外の古い版: 退避して作り直す（データは引き継がない）
SCHEMA_VERSION = 5
UPGRADABLE_FROM = {2, 3, 4}
# MIGRATIONS[v]: 版 v から v+1 に上げるために足す列 (テーブル, 列, 定義)。
# 新しいテーブルは SCHEMA の CREATE TABLE IF NOT EXISTS で作られる。列が既にあれば何もしない（何度流してもよい）
MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    2: [],  # v2 → v3: 問い（questions, question_assets）を追加（CREATE のみ）
    3: [  # v3 → v4: ユーザーにメールアドレスと説明を追加
        ("users", "email", "TEXT NOT NULL DEFAULT ''"),
        ("users", "description", "TEXT NOT NULL DEFAULT ''"),
    ],
    4: [  # v4 → v5: アセットにステータスを追加（既存のアセットは運用中になる）
        ("assets", "status", "TEXT NOT NULL DEFAULT '運用中' CHECK (status IN ('運用中', '非推奨', '廃止'))"),
    ],
}

log = logging.getLogger(__name__)

SCHEMA = """
-- ドメイン: オーナーを設定する基本の単位。アセットはドメインの割り当てを継承する
CREATE TABLE IF NOT EXISTS domains (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    file_path   TEXT NOT NULL,
    format      TEXT NOT NULL,
    row_count   INTEGER,
    description TEXT NOT NULL DEFAULT '',
    domain_id   INTEGER REFERENCES domains(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT '運用中' CHECK (status IN ('運用中', '非推奨', '廃止')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (asset_id, name)
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, tag_id)
);

CREATE TABLE IF NOT EXISTS column_tags (
    column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (column_id, tag_id)
);

CREATE TABLE IF NOT EXISTS lineage (
    upstream_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    downstream_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (upstream_id, downstream_id),
    CHECK (upstream_id <> downstream_id)
);

CREATE TABLE IF NOT EXISTS property_definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL CHECK (type IN ('text', 'longtext', 'number', 'date', 'select', 'url')),
    choices     TEXT NOT NULL DEFAULT '[]',  -- select 型の選択肢（JSON 配列）
    required    INTEGER NOT NULL DEFAULT 0,
    builtin_key TEXT UNIQUE,                 -- 標準プロパティの固定 ID。カスタムは NULL
    ordinal     INTEGER NOT NULL DEFAULT 0,
    help        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS asset_properties (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    property_id INTEGER NOT NULL REFERENCES property_definitions(id) ON DELETE CASCADE,
    value       TEXT NOT NULL,
    PRIMARY KEY (asset_id, property_id)
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    display_name         TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('admin', 'power', 'editor', 'viewer')),
    email                TEXT NOT NULL DEFAULT '',   -- 任意。入力されていれば他のユーザーと重複しない
    description          TEXT NOT NULL DEFAULT '',   -- 任意。所属・担当など
    active               INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_login_at        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users (email) WHERE email <> '';

-- 変更履歴。アセットやユーザーが消えても読めるよう、名前はその時点の値を文字列で持つ
CREATE TABLE IF NOT EXISTS change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime')),
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT,
    asset_id   INTEGER,
    asset_name TEXT,
    entity     TEXT NOT NULL,
    target     TEXT,
    action     TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_log_asset ON change_log (asset_id, at);
CREATE INDEX IF NOT EXISTS idx_change_log_at ON change_log (at);

-- 役割の種類。3層化するときは行を足すだけで済むよう、データとして持つ
CREATE TABLE IF NOT EXISTS role_types (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL UNIQUE,
    kind    TEXT NOT NULL CHECK (kind IN ('owner', 'steward')),
    ordinal INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO role_types (key, label, kind, ordinal) VALUES
    ('owner', 'オーナー', 'owner', 0),
    ('steward_business', 'スチュワード（業務）', 'steward', 1),
    ('steward_technical', 'スチュワード（技術）', 'steward', 2);

-- 役割割り当て（1行1割り当て）。対象はドメインかアセットのどちらか一方。
-- 継承元（ドメインから継承／個別設定）は持たず、表示時に計算する
CREATE TABLE IF NOT EXISTS role_assignments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id      INTEGER REFERENCES domains(id) ON DELETE CASCADE,
    asset_id       INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    role_type      TEXT NOT NULL REFERENCES role_types(key),
    org            TEXT NOT NULL,
    position       TEXT NOT NULL DEFAULT '',  -- オーナー: 役職（必須）
    person         TEXT NOT NULL DEFAULT '',  -- スチュワード: 担当者（必須）／オーナー: 現在の担当者（任意）
    scopes         TEXT NOT NULL DEFAULT '[]',  -- オーナー: 責任の範囲（JSON 配列）
    scope_note     TEXT NOT NULL DEFAULT '',  -- スチュワード: 担当の範囲
    delegation_ref TEXT NOT NULL DEFAULT '',  -- オーナー: 委譲規程への参照
    contact        TEXT NOT NULL DEFAULT '',  -- スチュワード: 問い合わせ窓口
    escalation     TEXT NOT NULL DEFAULT '',  -- スチュワード: エスカレーション先
    last_confirmed TEXT NOT NULL,             -- 最終確認日（YYYY-MM-DD）
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK ((domain_id IS NULL) <> (asset_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_role_assignments_asset ON role_assignments (asset_id);
CREATE INDEX IF NOT EXISTS idx_role_assignments_domain ON role_assignments (domain_id);

-- 問い: データが答えるべき問い。登録に必須なのは text だけで、ほかは後から育てる
CREATE TABLE IF NOT EXISTS questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,            -- Q-001 形式（自動採番）
    text             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT '' CHECK (kind IN ('', '分析', '業務')),
    requester_dept   TEXT NOT NULL DEFAULT '',
    requester        TEXT NOT NULL DEFAULT '',
    required_quality TEXT NOT NULL DEFAULT '',
    decision         TEXT NOT NULL DEFAULT '申請中' CHECK (decision IN ('申請中', '受諾', '条件付き受諾', '却下')),
    decided_by       TEXT NOT NULL DEFAULT '',
    decided_at       TEXT NOT NULL DEFAULT '',
    condition_note   TEXT NOT NULL DEFAULT '',
    monitor          TEXT NOT NULL DEFAULT '',
    quality_metric   TEXT NOT NULL DEFAULT '',
    review_date      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '有効' CHECK (status IN ('有効', '廃止')),
    created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS question_assets (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (question_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_question_assets_asset ON question_assets (asset_id);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """新しい接続を開く。接続はセッション・スレッド間で共有しないこと。

    スキーマの作成は init_db で別に行う（プロセスで1回でよい）。
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _backup_if_outdated(db_path: Path) -> Path | None:
    """版の違う既存 DB を退避する（破壊的マイグレーション）。退避先を返す。"""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    finally:
        conn.close()
    if not has_tables or version == SCHEMA_VERSION or version in UPGRADABLE_FROM:
        return None
    suffix = f".bak-{datetime.now():%Y%m%d%H%M%S}"
    backup = db_path.with_name(db_path.name + suffix)
    for ext in ("", "-wal", "-shm"):
        src = db_path.with_name(db_path.name + ext)
        if src.exists():
            src.rename(src.with_name(src.name + suffix))
    log.warning("スキーマの版が違うため、既存の DB を %s に退避して作り直しました（v%s → v%s）", backup, version, SCHEMA_VERSION)
    return backup


def _migrate(conn: sqlite3.Connection) -> None:
    """UPGRADABLE_FROM の版の DB を、MIGRATIONS の手順で SCHEMA_VERSION まで上げる。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in UPGRADABLE_FROM:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for v in range(version, SCHEMA_VERSION):
            for table, column, ddl in MIGRATIONS.get(v, []):
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.info("スキーマを v%s から v%s に上げました", version, SCHEMA_VERSION)


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path | None:
    """スキーマ作成・WAL 化・標準プロパティの登録を行う（何度呼んでもよい）。

    版の違う DB があれば退避して空の DB を作り、退避先を返す。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_if_outdated(db_path)
    conn = connect(db_path)
    try:
        # WAL: 読み込みが書き込みを待たず、書き込みも読み込みを待たない
        conn.execute("PRAGMA journal_mode = WAL")
        _migrate(conn)
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        seed_builtin(conn)
    finally:
        conn.close()
    return backup


def open_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """init_db してから接続を返す（スクリプトやテスト用）。"""
    init_db(db_path)
    return connect(db_path)
