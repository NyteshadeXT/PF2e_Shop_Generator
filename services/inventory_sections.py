"""Canonical inventory sections and shared snapshot/template transformations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class InventorySection:
    name: str
    list_key: str
    label: str


INVENTORY_SECTIONS = (
    InventorySection("mundane", "mundane_items", "Mundane"),
    InventorySection("materials", "material_items", "Materials"),
    InventorySection("formulas", "formula_items", "Formulas"),
    InventorySection("armor", "armor_items", "Armor/Shields"),
    InventorySection("weapons", "weapon_items", "Weapons"),
    InventorySection("magic", "magic_items", "Magic Items"),
)
SECTION_LIST_KEYS = {section.name: section.list_key for section in INVENTORY_SECTIONS}
INVENTORY_LIST_KEYS = tuple(section.list_key for section in INVENTORY_SECTIONS)
MERCHANDISE_LIST_KEYS = tuple(
    section.list_key for section in INVENTORY_SECTIONS if section.name != "formulas"
)


def inventory_lists(source: Mapping | None) -> dict[str, list]:
    """Return every canonical list key with an independent list value."""
    source = source if isinstance(source, Mapping) else {}
    return {key: list(source.get(key) or []) for key in INVENTORY_LIST_KEYS}


def legacy_snapshot_lists(snapshot: Mapping) -> dict[str, list]:
    """Normalize the flat snapshot layout used before nested inventory lists."""
    return inventory_lists(
        {
            "mundane_items": snapshot.get("mundane_items", []),
            "material_items": snapshot.get("material_items", [])
            or snapshot.get("materials_items", []),
            "formula_items": snapshot.get("formula_items", [])
            or snapshot.get("formulas_items", []),
            "armor_items": snapshot.get("armor_items", []),
            "weapon_items": snapshot.get("weapon_items", [])
            or snapshot.get("weapons_items", []),
            "magic_items": snapshot.get("magic_items", []),
        }
    )


def player_visible_lists(source: Mapping | None) -> dict[str, list]:
    """Copy inventory while excluding GM-hidden entries from every section."""
    return {
        key: [item for item in items if not item.get("player_hidden")]
        for key, items in inventory_lists(source).items()
    }


def flattened_inventory(source: Mapping | None, *, include_formulas: bool = False) -> list:
    normalized = inventory_lists(source)
    keys = INVENTORY_LIST_KEYS if include_formulas else MERCHANDISE_LIST_KEYS
    return [item for key in keys for item in normalized[key]]


def template_inventory_context(source: Mapping | None) -> dict[str, list]:
    """Build the consistently named list variables expected by result templates."""
    return inventory_lists(source)


def section_template_context(source: Mapping | None) -> list[dict]:
    """Pair canonical section metadata with its entries for section-driven controls."""
    normalized = inventory_lists(source)
    return [
        {
            "name": section.name,
            "list_key": section.list_key,
            "label": section.label,
            "entries": normalized[section.list_key],
        }
        for section in INVENTORY_SECTIONS
    ]


def section_counts(source: Mapping | None) -> dict[str, int]:
    normalized = inventory_lists(source)
    return {
        section.name: len(normalized[section.list_key]) for section in INVENTORY_SECTIONS
    }
