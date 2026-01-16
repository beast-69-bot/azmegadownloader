from __future__ import annotations

import sqlite3
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
