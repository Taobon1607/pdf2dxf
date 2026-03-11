"""
Database — SQLite để track usage + Pro users
Đơn giản, không cần ORM, đủ cho MVP
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/app.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            ip      TEXT NOT NULL,
            day     TEXT NOT NULL,
            count   INTEGER DEFAULT 0,
            PRIMARY KEY (ip, day)
        );

        CREATE TABLE IF NOT EXISTS pro_users (
            pro_key         TEXT PRIMARY KEY,
            stripe_customer TEXT,
            created_at      TEXT,
            active          INTEGER DEFAULT 1
        );
        """)


def get_usage(ip: str, day: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE ip=? AND day=?", (ip, day)
        ).fetchone()
        return row["count"] if row else 0


def increment_usage(ip: str, day: str):
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO usage (ip, day, count) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET count = count + 1
        """, (ip, day))


def is_pro_user(pro_key: str) -> bool:
    if not pro_key:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active FROM pro_users WHERE pro_key=?", (pro_key,)
        ).fetchone()
        return bool(row and row["active"])


def add_pro_user(pro_key: str, stripe_customer: str):
    from datetime import datetime
    with get_conn() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO pro_users (pro_key, stripe_customer, created_at, active)
        VALUES (?, ?, ?, 1)
        """, (pro_key, stripe_customer, datetime.utcnow().isoformat()))


def deactivate_pro_user(stripe_customer: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE pro_users SET active=0 WHERE stripe_customer=?",
            (stripe_customer,)
        )
