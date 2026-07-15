"""Portable, versioned keys for reproducing complete generation settings."""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .randomness import normalize_seed


PREFIX = "pf2e2."
LEGACY_PREFIX = "pf2e1."
MAX_KEY_LENGTH = 512


def create_reproduction_key(
    *,
    seed: str,
    shop_type: str,
    shop_size: str,
    disposition: str,
    party_level: int,
    fingerprint: str,
) -> str:
    fingerprint = str(fingerprint or "").strip()
    if not 8 <= len(fingerprint) <= 64 or not all(
        char.isalnum() or char in "_-" for char in fingerprint
    ):
        raise ValueError("Invalid generation fingerprint.")
    payload = {
        "d": str(disposition).strip(),
        "f": fingerprint,
        "l": int(party_level),
        "s": normalize_seed(seed),
        "t": str(shop_type).strip(),
        "z": str(shop_size).strip(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return PREFIX + encoded


def parse_reproduction_key(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if text.startswith(PREFIX):
        prefix = PREFIX
        expected_keys = {"d", "f", "l", "s", "t", "z"}
    elif text.startswith(LEGACY_PREFIX):
        prefix = LEGACY_PREFIX
        expected_keys = {"d", "l", "s", "t", "z"}
    else:
        return None
    if len(text) > MAX_KEY_LENGTH:
        raise ValueError("Invalid reproduction key.")
    try:
        encoded = text[len(prefix):]
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError
        if isinstance(payload["l"], bool):
            raise ValueError
        party_level = int(payload["l"])
        seed = str(payload["s"]).strip()
        if not seed:
            raise ValueError
        result = {
            "disposition": str(payload["d"]).strip(),
            "party_level": party_level,
            "seed": normalize_seed(seed),
            "shop_type": str(payload["t"]).strip(),
            "shop_size": str(payload["z"]).strip(),
            "_generation_fingerprint": str(payload.get("f") or "").strip(),
        }
        if not all(result[name] for name in ("disposition", "shop_type", "shop_size")):
            raise ValueError
        fingerprint = result["_generation_fingerprint"]
        if fingerprint and (
            not 8 <= len(fingerprint) <= 64
            or not all(char.isalnum() or char in "_-" for char in fingerprint)
        ):
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid reproduction key.") from exc
