"""Dependency-free semantic validation for the deployed SQLite catalog."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from services.money import parse_price_to_cp


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_VIEW_COLUMNS = {
    "category",
    "source_table",
    "source_id",
    "name",
    "level",
    "rarity",
    "type",
    "subtype",
    "price_text",
    "tags",
    "bulk",
    "source",
    "publisher_source",
    "shop_type",
    "stock_flag",
}
_REQUIRED_SOURCES = {
    "mundane",
    "weapon_basic",
    "armor_basic",
    "shield_basic",
    "runes",
    "scrolls",
    "staff_wand",
    "specific_magic_weapons",
    "specific_magic_armor",
    "specific_magic_shields",
}
_REQUIRED_TABLE_COLUMNS = {
    "Spells": {"name", "rank", "tradition", "rarity", "cost"},
    "Formula": {"itemlevel", "cost"},
    "Adjustments": {"name", "itemlevel", "subtype", "cost", "rarity"},
    "Armor_Material": {"name", "itemlevel", "addedprice", "prerequisite"},
    "Weapon_Material": {"name", "itemlevel", "addedprice", "prerequisite"},
    "Shield_Material": {"name", "itemlevel", "addedprice", "prerequisite"},
}


class CatalogValidationError(ValueError):
    """Raised when a catalog is readable SQLite but unsafe for generation."""


def _columns(connection: sqlite3.Connection, object_name: str) -> set[str]:
    escaped = object_name.replace('"', '""')
    return {
        str(row[1]).strip().lower()
        for row in connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    }


def _count(connection: sqlite3.Connection, query: str, parameters=()) -> int:
    return int(connection.execute(query, parameters).fetchone()[0])


def validate_catalog(
    database_path: str | Path,
    view_name: str,
    *,
    minimum_rows: int = 1_000,
) -> dict[str, Any]:
    """Validate catalog structure and generation-critical content."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise CatalogValidationError(f"Catalog file not found: {path}")
    if not _IDENTIFIER.fullmatch(str(view_name or "")):
        raise CatalogValidationError("Catalog view must be a simple SQLite identifier")

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise CatalogValidationError("Catalog failed SQLite integrity validation")

        objects = {
            str(row[0]).lower(): str(row[1]).lower()
            for row in connection.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        if objects.get(view_name.lower()) != "view":
            raise CatalogValidationError(f"Required catalog view is missing: {view_name}")

        view_columns = _columns(connection, view_name)
        missing_columns = sorted(_REQUIRED_VIEW_COLUMNS - view_columns)
        if missing_columns:
            raise CatalogValidationError(
                "Catalog view is missing columns: " + ", ".join(missing_columns)
            )

        for table, expected_columns in _REQUIRED_TABLE_COLUMNS.items():
            if objects.get(table.lower()) != "table":
                raise CatalogValidationError(f"Required catalog table is missing: {table}")
            missing = sorted(expected_columns - _columns(connection, table))
            if missing:
                raise CatalogValidationError(
                    f"Catalog table {table} is missing columns: {', '.join(missing)}"
                )

        quoted_view = '"' + view_name.replace('"', '""') + '"'
        rows = _count(connection, f"SELECT COUNT(*) FROM {quoted_view}")
        if rows < max(1, int(minimum_rows)):
            raise CatalogValidationError(
                f"Catalog contains {rows} rows; expected at least {minimum_rows}"
            )

        content_checks = {
            "blank item names": f"name IS NULL OR trim(name) = ''",
            "blank source tables": f"source_table IS NULL OR trim(source_table) = ''",
            "invalid levels": (
                "level IS NULL OR typeof(level) NOT IN ('integer', 'real') "
                "OR CAST(level AS REAL) < 0 OR CAST(level AS REAL) > 25"
            ),
            "invalid rarities": (
                "rarity IS NULL OR lower(trim(rarity)) NOT IN "
                "('common', 'uncommon', 'rare', 'unique')"
            ),
            "invalid stock flags": (
                "typeof(stock_flag) <> 'integer' OR stock_flag NOT IN (1, 2)"
            ),
        }
        failures = {
            label: _count(connection, f"SELECT COUNT(*) FROM {quoted_view} WHERE {where}")
            for label, where in content_checks.items()
        }
        failures = {label: count for label, count in failures.items() if count}
        if failures:
            detail = ", ".join(f"{label}: {count}" for label, count in failures.items())
            raise CatalogValidationError("Catalog contains invalid rows (" + detail + ")")

        duplicate_ids = _count(
            connection,
            f"""
            SELECT COUNT(*) FROM (
                SELECT source_table, source_id
                FROM {quoted_view}
                WHERE source_id IS NOT NULL AND trim(source_id) <> ''
                GROUP BY source_table, source_id
                HAVING COUNT(*) > 1
            )
            """,
        )
        if duplicate_ids:
            raise CatalogValidationError(
                f"Catalog contains {duplicate_ids} duplicated source identifiers"
            )

        source_counts = {
            str(source): int(count)
            for source, count in connection.execute(
                f"SELECT lower(trim(source_table)), COUNT(*) FROM {quoted_view} "
                "GROUP BY lower(trim(source_table))"
            )
        }
        missing_sources = sorted(source for source in _REQUIRED_SOURCES if not source_counts.get(source))
        if missing_sources:
            raise CatalogValidationError(
                "Catalog is missing generation-critical sources: "
                + ", ".join(missing_sources)
            )

        reference_counts = {
            table: _count(connection, f'SELECT COUNT(*) FROM "{table}"')
            for table in _REQUIRED_TABLE_COLUMNS
        }
        empty_references = sorted(table for table, count in reference_counts.items() if count <= 0)
        if empty_references:
            raise CatalogValidationError(
                "Catalog reference tables are empty: " + ", ".join(empty_references)
            )

        blank_prices = _count(
            connection,
            f"SELECT COUNT(*) FROM {quoted_view} "
            "WHERE price_text IS NULL OR trim(price_text) = ''",
        )
        distinct_prices = [
            str(row[0]).strip()
            for row in connection.execute(
                f"SELECT DISTINCT price_text FROM {quoted_view} "
                "WHERE price_text IS NOT NULL AND trim(price_text) <> ''"
            )
        ]
        invalid_prices = []
        for price in distinct_prices:
            parsed_price = parse_price_to_cp(price)
            if parsed_price is None or parsed_price < 0:
                invalid_prices.append(price)
        if invalid_prices:
            examples = ", ".join(repr(price) for price in invalid_prices[:5])
            raise CatalogValidationError(
                f"Catalog contains {len(invalid_prices)} invalid price values: {examples}"
            )

    return {
        "database": str(path),
        "view": view_name,
        "rows": rows,
        "sources": len(source_counts),
        "blank_prices": blank_prices,
        "distinct_prices": len(distinct_prices),
        "reference_rows": reference_counts,
    }
