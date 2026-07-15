"""Small dependency-free security helpers for the hosted application."""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Callable


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
