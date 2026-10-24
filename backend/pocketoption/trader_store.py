"""Small persistent store for Trader ID display preferences."""

import sqlite3
import os
from pathlib import Path

from timezone_utils import DEFAULT_TIMEZONE, normalize_timezone

DB_PATH = Path(os.environ.get(
    "TRADER_DB_PATH",
    str(Path(__file__).with_name("trader_preferences.sqlite3")),
))


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trader_preferences (
            trader_id TEXT PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return connection


def get_timezone(trader_id: str) -> str:
    with _connect() as connection:
        row = connection.execute(
            "SELECT timezone FROM trader_preferences WHERE trader_id = ?",
            (trader_id,),
        ).fetchone()
    return normalize_timezone(row[0] if row else DEFAULT_TIMEZONE)


def set_timezone(trader_id: str, timezone_name: str) -> str:
    timezone_name = normalize_timezone(timezone_name)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO trader_preferences (trader_id, timezone, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(trader_id) DO UPDATE SET
                timezone = excluded.timezone,
                updated_at = CURRENT_TIMESTAMP
            """,
            (trader_id, timezone_name),
        )
    return timezone_name
