"""Persistent storage for immutable Player View snapshots and live channels."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .settings import CONFIG


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOKEN_RE = re.compile(r"^[a-f0-9]{12,64}$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


class SnapshotNotFound(LookupError):
    """Raised when an immutable Player View snapshot does not exist."""


def normalize_channel(value: str | None) -> str:
    channel = (value or "default").strip().lower()
    if not _CHANNEL_RE.fullmatch(channel):
        raise ValueError("Channel must use 1-64 letters, numbers, underscores, or hyphens.")
    return channel


def normalize_token(value: str | None) -> str:
    token = (value or "").strip().lower()
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid Player View token.")
    return token


def state_db_path() -> Path:
    raw = os.environ.get("LOOTGEN_STATE_DB_PATH") or CONFIG.get(
        "player_view_db_path", "data/player_views.db"
    )
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _connection(db_path: str | Path | None = None):
    """Close SQLite connections explicitly; sqlite3's own context only commits."""
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def initialize(db_path: str | Path | None = None) -> None:
    with _connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS player_view_snapshots (
                token TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_player_view_snapshots_channel_created
                ON player_view_snapshots(channel, created_at DESC);

            CREATE TABLE IF NOT EXISTS player_view_channels (
                channel TEXT PRIMARY KEY,
                current_token TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(current_token) REFERENCES player_view_snapshots(token)
            );
            """
        )


def save_snapshot(
    token: str,
    channel: str,
    snapshot: dict[str, Any],
    *,
    db_path: str | Path | None = None,
) -> None:
    """Atomically store an immutable snapshot and advance its live channel."""
    token = normalize_token(token)
    channel = normalize_channel(channel)
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError("Snapshot must be a non-empty object.")
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    initialize(db_path)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO player_view_snapshots(token, channel, snapshot_json)
            VALUES (?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                channel = excluded.channel,
                snapshot_json = excluded.snapshot_json
            """,
            (token, channel, payload),
        )
        conn.execute(
            """
            INSERT INTO player_view_channels(channel, current_token, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel) DO UPDATE SET
                current_token = excluded.current_token,
                updated_at = CURRENT_TIMESTAMP
            """,
            (channel, token),
        )
        conn.commit()


def load_snapshot(
    token: str,
    channel: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    token = normalize_token(token)
    normalized_channel = normalize_channel(channel) if channel is not None else None
    initialize(db_path)
    with _connection(db_path) as conn:
        if normalized_channel is None:
            row = conn.execute(
                "SELECT snapshot_json FROM player_view_snapshots WHERE token = ?", (token,)
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT snapshot_json FROM player_view_snapshots
                WHERE token = ? AND channel = ?
                """,
                (token, normalized_channel),
            ).fetchone()
    if row is None:
        raise SnapshotNotFound(token)
    value = json.loads(row["snapshot_json"])
    if not isinstance(value, dict):
        raise ValueError("Stored Player View snapshot is invalid.")
    return value


def current_token(
    channel: str,
    *,
    db_path: str | Path | None = None,
) -> str:
    channel = normalize_channel(channel)
    initialize(db_path)
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT current_token FROM player_view_channels WHERE channel = ?", (channel,)
        ).fetchone()
    return str(row["current_token"]) if row else ""
