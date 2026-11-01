"""トランザクション管理。

接続は isolation_level=None（自動コミット）で開き、書き込みは write_tx の中で行う。
BEGIN IMMEDIATE で最初に書き込みロックを取るので、トランザクション内で読んだ値
（差分計算・循環チェック・最後の管理者チェックなど）は、コミットまで他の書き込みに変えられない。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager


@contextmanager
def write_tx(conn: sqlite3.Connection):
    if conn.in_transaction:  # 入れ子は外側のトランザクションに相乗りする
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
