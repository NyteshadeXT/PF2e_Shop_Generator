"""Single, validated configuration source for the application."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping, Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_OVERRIDES = {
    "LOOTGEN_DB_PATH": "sqlite_db_path",
    "LOOTGEN_STATE_DB_PATH": "player_view_db_path",
}


class ConfigurationError(ValueError):
    """Raised when config.json or an environment override is invalid."""


def _resolve_path(value: Any, root: Path) -> str | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def _validate_band(value: Any, location: str) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigurationError(f"{location} must be a [minimum, maximum] pair")
    try:
        low, high = int(value[0]), int(value[1])
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{location} values must be integers") from exc
    if low < 0 or high < 0 or high < low:
        raise ConfigurationError(f"{location} must satisfy 0 <= minimum <= maximum")


def validate_settings(config: dict[str, Any]) -> None:
    source = str(config.get("data_source", "sqlite")).strip().lower()
    if source != "sqlite":
        raise ConfigurationError("data_source must be 'sqlite'")
    if not config.get("sqlite_db_path"):
        raise ConfigurationError("sqlite_db_path is required for the sqlite data source")

    view = str(config.get("sqlite_view", "")).strip()
    if not _SQL_IDENTIFIER.fullmatch(view):
        raise ConfigurationError("sqlite_view must be a simple SQL identifier")

    caps = config.get("level_caps", {})
    try:
        cap_min, cap_max = int(caps.get("min", 1)), int(caps.get("max", 20))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("level_caps values must be integers") from exc
    if cap_min < 0 or cap_max < cap_min:
        raise ConfigurationError("level_caps must satisfy 0 <= min <= max")

    multipliers = config.get("disposition_multipliers")
    if not isinstance(multipliers, dict) or not multipliers:
        raise ConfigurationError("disposition_multipliers must be a non-empty object")
    for name, value in multipliers.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"disposition_multipliers.{name} must be numeric") from exc
        if number <= 0:
            raise ConfigurationError(f"disposition_multipliers.{name} must be greater than zero")

    for size, block in (config.get("counts") or {}).items():
        if not isinstance(block, dict):
            raise ConfigurationError(f"counts.{size} must be an object")
        for item_type, band in block.items():
            _validate_band(band, f"counts.{size}.{item_type}")
    for shop, sizes in (config.get("counts_by_shop") or {}).items():
        if not isinstance(sizes, dict):
            raise ConfigurationError(f"counts_by_shop.{shop} must be an object")
        for size, block in sizes.items():
            if not isinstance(block, dict):
                raise ConfigurationError(f"counts_by_shop.{shop}.{size} must be an object")
            for item_type, band in block.items():
                _validate_band(band, f"counts_by_shop.{shop}.{size}.{item_type}")

    groups = config.get("source_table_groups")
    if not isinstance(groups, dict) or not groups:
        raise ConfigurationError("source_table_groups must be a non-empty object")
    for name, values in groups.items():
        if not isinstance(values, list) or not any(str(v).strip() for v in values):
            raise ConfigurationError(f"source_table_groups.{name} must be a non-empty list")

    player_views = config.get("player_views", {}) or {}
    if not isinstance(player_views, dict):
        raise ConfigurationError("player_views must be an object")
    for key, default in (("retention_days", 365), ("max_snapshots_per_channel", 250)):
        try:
            value = int(player_views.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"player_views.{key} must be an integer") from exc
        if value < 0:
            raise ConfigurationError(f"player_views.{key} must be zero or greater")


def load_settings(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    path = path.resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"Configuration file not found: {path}. "
            "Ensure config.json is committed to GitHub and deployed beside app.py."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigurationError("The configuration root must be an object")

    environment = os.environ if env is None else env
    for env_name, key in _ENV_OVERRIDES.items():
        value = environment.get(env_name)
        if value is not None and str(value).strip():
            config[key] = str(value).strip()

    root = path.parent
    for key in ("sqlite_db_path", "player_view_db_path"):
        if key in config:
            config[key] = _resolve_path(config.get(key), root)

    validate_settings(config)
    return config


CONFIG = load_settings()
