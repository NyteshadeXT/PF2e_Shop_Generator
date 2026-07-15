"""Small dependency-free security helpers for the hosted application."""
from __future__ import annotations

import time
import os
import secrets
import hashlib
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from threading import Lock
from typing import Callable


def load_session_secret(
    configured_secret: str | None,
    fallback_path: str | Path,
    *,
    wait_attempts: int = 50,
) -> str:
    """Return an explicit secret or atomically create a worker-shared fallback."""
    explicit = str(configured_secret or "").strip()
    if explicit:
        return explicit

    path = Path(fallback_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    except OSError as exc:
        raise RuntimeError(
            "Unable to create the shared session-secret file. Set "
            "LOOTGEN_SESSION_SECRET to a long random value."
        ) from exc
    else:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as secret_file:
                secret_file.write(candidate + "\n")
                secret_file.flush()
                os.fsync(secret_file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return candidate

    # Another worker may have won O_EXCL but not completed its write yet.
    for _attempt in range(max(1, int(wait_attempts))):
        try:
            saved = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            saved = ""
        if len(saved) >= 32:
            return saved
        time.sleep(0.01)
    raise RuntimeError(
        f"The shared session-secret file is missing or invalid: {path}. "
        "Set LOOTGEN_SESSION_SECRET to a long random value."
    )


class AttemptLimiter:
    """Bound failed attempts by client within a rolling time window."""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.max_clients = max(100, int(max_clients))
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _purge(self, attempts: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def blocked(self, client: str) -> bool:
        key = str(client or "unknown")[:200]
        now = self._clock()
        with self._lock:
            attempts = self._failures.get(key)
            if attempts is None:
                return False
            self._purge(attempts, now)
            if not attempts:
                self._failures.pop(key, None)
                return False
            return len(attempts) >= self.max_attempts

    def record_failure(self, client: str) -> None:
        key = str(client or "unknown")[:200]
        now = self._clock()
        with self._lock:
            attempts = self._failures.setdefault(key, deque())
            self._purge(attempts, now)
            attempts.append(now)
            if len(self._failures) > self.max_clients:
                for candidate in list(self._failures):
                    values = self._failures[candidate]
                    self._purge(values, now)
                    if not values:
                        self._failures.pop(candidate, None)
                while len(self._failures) > self.max_clients:
                    self._failures.pop(next(iter(self._failures)))

    def clear(self, client: str) -> None:
        key = str(client or "unknown")[:200]
        with self._lock:
            self._failures.pop(key, None)


class SQLiteAttemptLimiter:
    """Enforce one rolling failure limit across all application workers."""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        db_path: str | Path,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.max_clients = max(100, int(max_clients))
        self.db_path = Path(db_path).expanduser().resolve()
        self._clock = clock
        self._schema_lock = Lock()

    @staticmethod
    def _client_key(client: str) -> str:
        normalized = str(client or "unknown")[:200].encode("utf-8", errors="replace")
        return hashlib.sha256(normalized).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        # Backups can replace the state database while a worker remains alive,
        # so verify the small limiter schema on every login operation.
        with self._schema_lock:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS gm_login_failures (
                            client_key TEXT NOT NULL,
                            attempted_at REAL NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_gm_login_failures_client_time
                        ON gm_login_failures (client_key, attempted_at)
                        """
                    )

    def blocked(self, client: str) -> bool:
        self._initialize()
        now = float(self._clock())
        cutoff = now - self.window_seconds
        key = self._client_key(client)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM gm_login_failures WHERE attempted_at <= ?", (cutoff,)
                )
                count = connection.execute(
                    "SELECT COUNT(*) FROM gm_login_failures WHERE client_key = ?",
                    (key,),
                ).fetchone()[0]
        return int(count) >= self.max_attempts

    def record_failure(self, client: str) -> None:
        self._initialize()
        now = float(self._clock())
        cutoff = now - self.window_seconds
        key = self._client_key(client)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM gm_login_failures WHERE attempted_at <= ?", (cutoff,)
                )
                connection.execute(
                    "INSERT INTO gm_login_failures (client_key, attempted_at) VALUES (?, ?)",
                    (key, now),
                )
                client_count = connection.execute(
                    "SELECT COUNT(DISTINCT client_key) FROM gm_login_failures"
                ).fetchone()[0]
                if int(client_count) > self.max_clients:
                    connection.execute(
                        """
                        DELETE FROM gm_login_failures
                        WHERE client_key IN (
                            SELECT client_key
                            FROM gm_login_failures
                            GROUP BY client_key
                            ORDER BY MAX(attempted_at) ASC
                            LIMIT ?
                        )
                        """,
                        (int(client_count) - self.max_clients,),
                    )

    def clear(self, client: str) -> None:
        self._initialize()
        key = self._client_key(client)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM gm_login_failures WHERE client_key = ?", (key,)
                )
