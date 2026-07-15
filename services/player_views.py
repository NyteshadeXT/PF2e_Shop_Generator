"""Persistent storage for immutable Player View snapshots and live channels."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .settings import CONFIG


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOKEN_RE = re.compile(r"^[a-f0-9]{12,64}$", re.IGNORECASE)
_LIVE_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


class SnapshotNotFound(LookupError):
    """Raised when an immutable Player View snapshot does not exist."""


class LiveChannelNotFound(LookupError):
    """Raised when a live-display capability token does not exist."""


class SnapshotConflict(ValueError):
    """Raised when a token is reused for different immutable snapshot data."""


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


def normalize_live_token(value: str | None) -> str:
    token = (value or "").strip().lower()
    if not _LIVE_TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid live-display token.")
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
                live_token TEXT UNIQUE,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(current_token) REFERENCES player_view_snapshots(token)
            );
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(player_view_channels)").fetchall()
        }
        if "live_token" not in columns:
            conn.execute("ALTER TABLE player_view_channels ADD COLUMN live_token TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_player_view_channels_live_token
            ON player_view_channels(live_token)
            WHERE live_token IS NOT NULL
            """
        )
        conn.commit()


def _cleanup_in_connection(
    conn: sqlite3.Connection,
    *,
    retention_days: int,
    max_snapshots_per_channel: int,
) -> int:
    """Delete expired/excess snapshots without ever deleting a live current snapshot."""
    before = conn.total_changes
    if retention_days > 0:
        conn.execute(
            """
            DELETE FROM player_view_snapshots
            WHERE created_at < datetime('now', ?)
              AND token NOT IN (
                  SELECT current_token FROM player_view_channels
              )
            """,
            (f"-{retention_days} days",),
        )
    if max_snapshots_per_channel > 0:
        conn.execute(
            """
            WITH ranked AS (
                SELECT
                    token,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel
                        ORDER BY created_at DESC, rowid DESC
                    ) AS position
                FROM player_view_snapshots
            )
            DELETE FROM player_view_snapshots
            WHERE token IN (
                SELECT token FROM ranked WHERE position > ?
            )
              AND token NOT IN (
                  SELECT current_token FROM player_view_channels
              )
            """,
            (max_snapshots_per_channel,),
        )
    return conn.total_changes - before


def _retention_values(
    retention_days: int | None = None,
    max_snapshots_per_channel: int | None = None,
) -> tuple[int, int]:
    cfg = CONFIG.get("player_views", {}) or {}
    days = int(cfg.get("retention_days", 365) if retention_days is None else retention_days)
    maximum = int(
        cfg.get("max_snapshots_per_channel", 250)
        if max_snapshots_per_channel is None
        else max_snapshots_per_channel
    )
    if days < 0 or maximum < 0:
        raise ValueError("Player View retention values must be zero or greater.")
    return days, maximum


def cleanup_snapshots(
    *,
    db_path: str | Path | None = None,
    retention_days: int | None = None,
    max_snapshots_per_channel: int | None = None,
    vacuum: bool = False,
) -> int:
    """Run manual retention cleanup and optionally compact the state database."""
    days, maximum = _retention_values(retention_days, max_snapshots_per_channel)
    initialize(db_path)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        removed = _cleanup_in_connection(
            conn,
            retention_days=days,
            max_snapshots_per_channel=maximum,
        )
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    return removed


def snapshot_stats(*, db_path: str | Path | None = None) -> dict[str, int | str | None]:
    initialize(db_path)
    with _connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS snapshots, MIN(created_at) AS oldest, MAX(created_at) AS newest
            FROM player_view_snapshots
            """
        ).fetchone()
        channels = conn.execute("SELECT COUNT(*) FROM player_view_channels").fetchone()[0]
    return {
        "snapshots": int(row["snapshots"]),
        "channels": int(channels),
        "oldest": row["oldest"],
        "newest": row["newest"],
    }


def backup_database(
    output_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> Path:
    """Create an atomic, integrity-checked backup using SQLite's online backup API."""
    source = (Path(db_path) if db_path is not None else state_db_path()).resolve()
    destination = Path(output_path).expanduser().resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the active state database.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{secrets.token_hex(8)}.tmp"
    )
    initialize(source)
    try:
        with _connection(source) as source_connection:
            backup_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(backup_connection)
                result = backup_connection.execute("PRAGMA integrity_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise sqlite3.DatabaseError("Player View backup failed its integrity check.")
            finally:
                backup_connection.close()
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def recent_snapshots(
    *,
    channel: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return recent snapshots with enough metadata for a GM recovery screen."""
    normalized_channel = normalize_channel(channel) if channel else None
    safe_limit = max(1, min(int(limit), 200))
    initialize(db_path)
    sql = """
        SELECT
            s.token,
            s.channel,
            s.snapshot_json,
            s.created_at,
            CASE WHEN c.current_token = s.token THEN 1 ELSE 0 END AS is_current,
            c.live_token
        FROM player_view_snapshots AS s
        LEFT JOIN player_view_channels AS c ON c.channel = s.channel
    """
    parameters: list[Any] = []
    if normalized_channel:
        sql += " WHERE s.channel = ?"
        parameters.append(normalized_channel)
    sql += " ORDER BY s.created_at DESC, s.rowid DESC LIMIT ?"
    parameters.append(safe_limit)

    with _connection(db_path) as conn:
        rows = conn.execute(sql, parameters).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["snapshot_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        shop = payload.get("shop") if isinstance(payload, dict) else {}
        if not isinstance(shop, dict):
            shop = {}
        results.append(
            {
                "token": str(row["token"]),
                "channel": str(row["channel"]),
                "created_at": str(row["created_at"]),
                "is_current": bool(row["is_current"]),
                "live_token": str(row["live_token"] or ""),
                "shop_name": str(shop.get("shop_name") or shop.get("name") or ""),
                "shop_type": str(shop.get("shop_type") or ""),
                "shop_size": str(shop.get("shop_size") or ""),
                "party_level": shop.get("party_level"),
                "seed": str(shop.get("seed") or ""),
            }
        )
    return results


def set_current_snapshot(
    token: str,
    channel: str,
    *,
    db_path: str | Path | None = None,
) -> str:
    """Point a channel's stable Live Display at an existing immutable snapshot."""
    token = normalize_token(token)
    channel = normalize_channel(channel)
    proposed_live_token = secrets.token_hex(16)
    initialize(db_path)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM player_view_snapshots WHERE token = ? AND channel = ?",
            (token, channel),
        ).fetchone()
        if exists is None:
            raise SnapshotNotFound(token)
        conn.execute(
            """
            INSERT INTO player_view_channels(channel, current_token, live_token, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel) DO UPDATE SET
                current_token = excluded.current_token,
                updated_at = CURRENT_TIMESTAMP
            """,
            (channel, token, proposed_live_token),
        )
        conn.execute(
            """
            UPDATE player_view_channels
            SET live_token = ?
            WHERE channel = ? AND (live_token IS NULL OR live_token = '')
            """,
            (proposed_live_token, channel),
        )
        row = conn.execute(
            "SELECT live_token FROM player_view_channels WHERE channel = ?", (channel,)
        ).fetchone()
        conn.commit()
    return str(row["live_token"])


def save_snapshot(
    token: str,
    channel: str,
    snapshot: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    advance_channel: bool = True,
) -> str:
    """Atomically store an immutable snapshot and advance its live channel."""
    token = normalize_token(token)
    channel = normalize_channel(channel)
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError("Snapshot must be a non-empty object.")
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    proposed_live_token = secrets.token_hex(16)
    retention_days, max_snapshots = _retention_values()

    initialize(db_path)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT channel, snapshot_json FROM player_view_snapshots WHERE token = ?",
            (token,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO player_view_snapshots(token, channel, snapshot_json)
                VALUES (?, ?, ?)
                """,
                (token, channel, payload),
            )
        elif existing["channel"] != channel or existing["snapshot_json"] != payload:
            raise SnapshotConflict("Player View tokens cannot be reused for different snapshots.")
        if advance_channel:
            conn.execute(
                """
                INSERT INTO player_view_channels(channel, current_token, live_token, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel) DO UPDATE SET
                    current_token = excluded.current_token,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (channel, token, proposed_live_token),
            )
        else:
            conn.execute(
                """
                INSERT INTO player_view_channels(channel, current_token, live_token, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel) DO NOTHING
                """,
                (channel, token, proposed_live_token),
            )
        conn.execute(
            """
            UPDATE player_view_channels
            SET live_token = ?
            WHERE channel = ? AND (live_token IS NULL OR live_token = '')
            """,
            (proposed_live_token, channel),
        )
        row = conn.execute(
            "SELECT live_token FROM player_view_channels WHERE channel = ?", (channel,)
        ).fetchone()
        _cleanup_in_connection(
            conn,
            retention_days=retention_days,
            max_snapshots_per_channel=max_snapshots,
        )
        conn.commit()
    return str(row["live_token"])


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


def live_channel(
    live_token: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, str]:
    """Resolve a secret live-display token to its channel and newest snapshot."""
    live_token = normalize_live_token(live_token)
    initialize(db_path)
    with _connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT channel, current_token
            FROM player_view_channels
            WHERE live_token = ?
            """,
            (live_token,),
        ).fetchone()
    if row is None:
        raise LiveChannelNotFound(live_token)
    return {"channel": str(row["channel"]), "roll_id": str(row["current_token"])}


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Maintain persistent Player View snapshots")
    parser.add_argument("command", choices=("backup", "cleanup", "stats"))
    parser.add_argument("--vacuum", action="store_true", help="Compact the database after cleanup")
    parser.add_argument("--output", help="Destination database path for the backup command")
    args = parser.parse_args()
    if args.command == "backup":
        if not args.output:
            parser.error("backup requires --output")
        destination = backup_database(args.output)
        print(f"Player View backup created: {destination}")
    elif args.command == "cleanup":
        removed = cleanup_snapshots(vacuum=args.vacuum)
        print(f"Removed {removed} Player View snapshot(s).")
    else:
        stats = snapshot_stats()
        for key, value in stats.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    _main()
