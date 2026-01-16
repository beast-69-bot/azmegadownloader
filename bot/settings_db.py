from __future__ import annotations

# Adapted from WZML-X (wzv3) user settings patterns:
# https://github.com/WZML-X/WZML/tree/wzv3

import json
import sqlite3
from pathlib import Path

DEFAULT_SETTINGS = {
    "TYPE": False,
    "THUMB": False,
    "SPLIT_SIZE": 0,
    "EQUAL": False,
    "GROUP": False,
    "DESTINATION": "",
    "PREFIX": "",
    "SUFFIX": "",
    "CAPTION": "",
    "LAYOUT": "",
    "THUMB_PATH": "",
}

DB_PATH = Path(__file__).resolve().parent.parent / "settings.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                settings TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_settings(user_id: int) -> dict:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT settings FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row and row[0]:
        try:
            settings.update(json.loads(row[0]))
        except json.JSONDecodeError:
            pass
    return settings


def save_settings(user_id: int, settings: dict) -> None:
    init_db()
    data = json.dumps(settings)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, settings)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET settings = excluded.settings
            """,
            (user_id, data),
        )
        conn.commit()
