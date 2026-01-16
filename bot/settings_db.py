from __future__ import annotations

import sqlite3
import time
import secrets
from pathlib import Path

DEFAULT_SETTINGS = {
    "chat_id": "",
    "caption": "",
    "thumb_path": "",
}

DB_PATH = Path(__file__).resolve().parent.parent / "settings.db"


def _ensure_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                chat_id TEXT,
                caption TEXT,
                thumb_path TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS global_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_status (
                user_id INTEGER PRIMARY KEY,
                verify_status_ts INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_tokens (
                user_id INTEGER,
                token TEXT,
                created_at INTEGER,
                expire_at INTEGER,
                PRIMARY KEY (user_id, token)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_bans (
                user_id INTEGER PRIMARY KEY,
                strikes INTEGER,
                banned INTEGER
            )
            """
        )
        conn.commit()


def get_settings(user_id: int) -> dict:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT chat_id, caption, thumb_path FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row:
        settings["chat_id"] = row[0] or ""
        settings["caption"] = row[1] or ""
        settings["thumb_path"] = row[2] or ""
    return settings


def save_settings(user_id: int, settings: dict) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, chat_id, caption, thumb_path)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                caption = excluded.caption,
                thumb_path = excluded.thumb_path
            """,
            (
                user_id,
                settings.get("chat_id", ""),
                settings.get("caption", ""),
                settings.get("thumb_path", ""),
            ),
        )
        conn.commit()


def parse_chat_target(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    if "/" in value:
        chat_id_str, topic_id_str = value.split("/", 1)
        if chat_id_str.lstrip("-").isdigit() and topic_id_str.isdigit():
            return int(chat_id_str), int(topic_id_str)
        return None, None
    if value.lstrip("-").isdigit():
        return int(value), None
    return None, None


def get_global_setting(key: str) -> str:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row[0] if row else ""


def set_global_setting(key: str, value: str) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO global_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_admin_ids() -> set[int]:
    raw = get_global_setting("admin_user_ids")
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def add_admin_id(user_id: int) -> None:
    admins = get_admin_ids()
    admins.add(user_id)
    set_global_setting("admin_user_ids", ",".join(str(x) for x in sorted(admins)))


def remove_admin_id(user_id: int) -> None:
    admins = get_admin_ids()
    admins.discard(user_id)
    set_global_setting("admin_user_ids", ",".join(str(x) for x in sorted(admins)))


def create_verify_token(user_id: int, ttl: int) -> dict:
    _ensure_db()
    token = secrets.token_urlsafe(10)
    now = int(time.time())
    expire_at = now + max(int(ttl or 0), 0)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO verify_tokens (user_id, token, created_at, expire_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, token, now, expire_at),
        )
        conn.commit()
    return {"token": token, "created_at": now, "expire_at": expire_at}


def get_verify_token(user_id: int, token: str) -> dict | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT token, created_at, expire_at
            FROM verify_tokens
            WHERE user_id = ? AND token = ?
            """,
            (user_id, token),
        ).fetchone()
    if not row:
        return None
    return {"token": row[0], "created_at": row[1], "expire_at": row[2]}


def delete_verify_token(user_id: int, token: str) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM verify_tokens WHERE user_id = ? AND token = ?",
            (user_id, token),
        )
        conn.commit()


def clear_verify_tokens(user_id: int) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM verify_tokens WHERE user_id = ?", (user_id,))
        conn.commit()


def set_verify_status(user_id: int, ts: int | None = None) -> None:
    _ensure_db()
    ts = int(ts or time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO verify_status (user_id, verify_status_ts)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET verify_status_ts = excluded.verify_status_ts
            """,
            (user_id, ts),
        )
        conn.commit()


def get_verify_status(user_id: int) -> int | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT verify_status_ts FROM verify_status WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else None


def clear_verify_status(user_id: int) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM verify_status WHERE user_id = ?", (user_id,))
        conn.commit()


def record_verify_strike(user_id: int) -> tuple[int, bool]:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT strikes, banned FROM verify_bans WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        strikes = int(row[0]) if row else 0
        strikes += 1
        banned = 1 if strikes >= 2 else 0
        conn.execute(
            """
            INSERT INTO verify_bans (user_id, strikes, banned)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET strikes = excluded.strikes, banned = excluded.banned
            """,
            (user_id, strikes, banned),
        )
        conn.commit()
    return strikes, bool(banned)


def clear_verify_strikes(user_id: int) -> None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM verify_bans WHERE user_id = ?", (user_id,))
        conn.commit()


def is_user_banned(user_id: int) -> bool:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT banned FROM verify_bans WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return bool(row and int(row[0]))
