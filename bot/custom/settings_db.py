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
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER PRIMARY KEY,
                is_premium INTEGER,
                premium_expire_ts INTEGER,
                daily_task_count INTEGER,
                last_task_date TEXT,
                is_verified INTEGER,
                verification_fail_count INTEGER,
                verification_blocked INTEGER,
                is_banned INTEGER
            )
            """
        )
        # Backfill for existing DBs without premium_expire_ts
        try:
            conn.execute("ALTER TABLE user_limits ADD COLUMN premium_expire_ts INTEGER")
        except sqlite3.OperationalError:
            pass
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_tokens (
                token TEXT PRIMARY KEY,
                created_at INTEGER,
                expires_at INTEGER,
                redeemed_by INTEGER,
                redeemed_at INTEGER,
                generated_by INTEGER
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


def _ensure_user_limits(user_id: int) -> dict:
    _ensure_db()
    defaults = {
        "is_premium": 0,
        "premium_expire_ts": 0,
        "daily_task_count": 0,
        "last_task_date": "",
        "is_verified": 0,
        "verification_fail_count": 0,
        "verification_blocked": 0,
        "is_banned": 0,
    }
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT is_premium, premium_expire_ts, daily_task_count, last_task_date,
                   is_verified, verification_fail_count, verification_blocked, is_banned
            FROM user_limits WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO user_limits (
                    user_id, is_premium, premium_expire_ts, daily_task_count, last_task_date,
                    is_verified, verification_fail_count, verification_blocked, is_banned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    defaults["is_premium"],
                    defaults["premium_expire_ts"],
                    defaults["daily_task_count"],
                    defaults["last_task_date"],
                    defaults["is_verified"],
                    defaults["verification_fail_count"],
                    defaults["verification_blocked"],
                    defaults["is_banned"],
                ),
            )
            conn.commit()
            return dict(defaults)
    return {
        "is_premium": int(row[0] or 0),
        "premium_expire_ts": int(row[1] or 0),
        "daily_task_count": int(row[2] or 0),
        "last_task_date": row[3] or "",
        "is_verified": int(row[4] or 0),
        "verification_fail_count": int(row[5] or 0),
        "verification_blocked": int(row[6] or 0),
        "is_banned": int(row[7] or 0),
    }


def get_user_limits_snapshot(user_id: int) -> dict:
    return _ensure_user_limits(user_id)


def get_daily_task_count_snapshot(user_id: int, today: str) -> int:
    data = _ensure_user_limits(user_id)
    if data.get("last_task_date") != today:
        return 0
    return int(data.get("daily_task_count", 0))


def update_user_limits(user_id: int, **fields) -> None:
    _ensure_db()
    _ensure_user_limits(user_id)
    allowed = {
        "is_premium",
        "premium_expire_ts",
        "daily_task_count",
        "last_task_date",
        "is_verified",
        "verification_fail_count",
        "verification_blocked",
        "is_banned",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    keys = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [user_id]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE user_limits SET {keys} WHERE user_id = ?", values)
        conn.commit()


def is_premium(user_id: int) -> bool:
    data = _ensure_user_limits(user_id)
    if not int(data.get("is_premium", 0)):
        return False
    exp = int(data.get("premium_expire_ts", 0))
    if exp and int(time.time()) > exp:
        update_user_limits(user_id, is_premium=0, premium_expire_ts=0)
        return False
    return True


def set_premium(user_id: int, enabled: bool, expire_ts: int = 0) -> None:
    if enabled:
        update_user_limits(user_id, is_premium=1, premium_expire_ts=int(expire_ts or 0))
    else:
        update_user_limits(user_id, is_premium=0, premium_expire_ts=0)


def list_premium_users() -> list[int]:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM user_limits WHERE is_premium = 1"
        ).fetchall()
    return [int(row[0]) for row in rows]


def get_premium_expire_ts(user_id: int) -> int:
    data = _ensure_user_limits(user_id)
    return int(data.get("premium_expire_ts", 0))


def is_globally_banned(user_id: int) -> bool:
    data = _ensure_user_limits(user_id)
    return bool(int(data.get("is_banned", 0)))


def set_global_ban(user_id: int, enabled: bool) -> None:
    update_user_limits(user_id, is_banned=1 if enabled else 0)


def list_banned_users() -> list[int]:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM user_limits WHERE is_banned = 1"
        ).fetchall()
    return [int(row[0]) for row in rows]


def create_premium_tokens(qty: int, generated_by: int, ttl_seconds: int = 3600) -> list[str]:
    _ensure_db()
    now = int(time.time())
    tokens: list[str] = []
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    with sqlite3.connect(DB_PATH) as conn:
        for _ in range(qty):
            while True:
                token = "PREM-" + "".join(secrets.choice(alphabet) for _ in range(6))
                expires_at = now + ttl_seconds
                try:
                    conn.execute(
                        """
                        INSERT INTO premium_tokens
                            (token, created_at, expires_at, redeemed_by, redeemed_at, generated_by)
                        VALUES (?, ?, ?, NULL, NULL, ?)
                        """,
                        (token, now, expires_at, generated_by),
                    )
                    tokens.append(token)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    return tokens


def get_premium_token(token: str) -> dict | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT token, created_at, expires_at, redeemed_by, redeemed_at, generated_by
            FROM premium_tokens WHERE token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        return None
    return {
        "token": row[0],
        "created_at": int(row[1] or 0),
        "expires_at": int(row[2] or 0),
        "redeemed_by": row[3],
        "redeemed_at": row[4],
        "generated_by": row[5],
    }


def mark_premium_token_redeemed(token: str, user_id: int, redeemed_at: int | None = None) -> None:
    _ensure_db()
    if redeemed_at is None:
        redeemed_at = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE premium_tokens
            SET redeemed_by = ?, redeemed_at = ?
            WHERE token = ? AND redeemed_by IS NULL
            """,
            (user_id, redeemed_at, token),
        )
        conn.commit()


def get_daily_task_count(user_id: int, today: str) -> int:
    data = _ensure_user_limits(user_id)
    if data.get("last_task_date") != today:
        update_user_limits(user_id, daily_task_count=0, last_task_date=today)
        return 0
    return int(data.get("daily_task_count", 0))


def increment_daily_task_count(user_id: int, today: str) -> int:
    count = get_daily_task_count(user_id, today)
    count += 1
    update_user_limits(user_id, daily_task_count=count, last_task_date=today)
    return count


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
    update_user_limits(user_id, is_verified=1)


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
    update_user_limits(user_id, is_verified=0)


def record_verify_strike(user_id: int) -> tuple[int, bool]:
    _ensure_db()
    data = _ensure_user_limits(user_id)
    strikes = int(data.get("verification_fail_count", 0)) + 1
    banned = 1 if strikes >= 3 else 0
    update_user_limits(
        user_id,
        verification_fail_count=strikes,
        verification_blocked=banned,
    )
    return strikes, bool(banned)


def clear_verify_strikes(user_id: int) -> None:
    update_user_limits(user_id, verification_fail_count=0, verification_blocked=0)


def is_user_banned(user_id: int) -> bool:
    data = _ensure_user_limits(user_id)
    return bool(int(data.get("verification_blocked", 0)))
