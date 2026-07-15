"""Request-local deterministic randomness for reproducible generation."""
from __future__ import annotations

import random
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_current_rng: ContextVar[random.Random | None] = ContextVar("generation_rng", default=None)
_fallback_rng = random.Random()


def normalize_seed(value: object | None) -> str:
    """Return a user-facing seed, creating a secure one when none was supplied."""
    seed = str(value or "").strip()
    if not seed:
        return secrets.token_hex(8)
    if len(seed) > 64:
        raise ValueError("Generation seed must be 64 characters or fewer.")
    if any(ord(char) < 32 for char in seed):
        raise ValueError("Generation seed cannot contain control characters.")
    return seed


def get_rng() -> random.Random:
    """Return the RNG for the current request without sharing request state."""
    return _current_rng.get() or _fallback_rng


@contextmanager
def generation_rng(seed: str) -> Iterator[random.Random]:
    rng = random.Random(seed)
    token = _current_rng.set(rng)
    try:
        yield rng
    finally:
        _current_rng.reset(token)
