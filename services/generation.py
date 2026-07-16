"""Framework-neutral orchestration for deterministic shop generation."""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from services.inventory_sections import flattened_inventory, inventory_lists
from services.logic import (
    CONFIG,
    select_armor_items,
    select_formulas,
    select_magic_items,
    select_materials,
    select_mundane_items,
    select_specific_magic_armor,
    select_specific_magic_weapons,
    select_weapons_items,
)
from services.provenance import generation_fingerprint
from services.randomness import generation_rng, normalize_seed
from services.reproduction import create_reproduction_key, parse_reproduction_key
from services.spellbooks import select_spellbooks
from services.utils import rarity_counts


class GenerationInputError(ValueError):
    """A submitted generation setting is invalid."""


def count_critical(items) -> int:
    return sum(1 for item in (items or []) if item.get("critical"))


def get_shop_types(df: pd.DataFrame) -> list[str]:
    if "shop_type" in df.columns and df["shop_type"].dropna().size:
        return sorted(str(value) for value in df["shop_type"].dropna().unique())
    return list(CONFIG.get("default_shop_types", []))


def _canonical_choice(value, choices, label: str, default: str) -> str:
    requested = str(value or default).strip()
    canonical = {str(choice).casefold(): str(choice) for choice in choices}
    selected = canonical.get(requested.casefold())
    if selected is None:
        raise GenerationInputError(f"Invalid {label}.")
    return selected


def validate_generation_inputs(data: Mapping, df: pd.DataFrame):
    shop_type = _canonical_choice(
        data.get("shop_type"), get_shop_types(df), "shop type", "General"
    )
    shop_size = _canonical_choice(
        data.get("shop_size"), (CONFIG.get("counts") or {}).keys(), "shop size", "medium"
    ).lower()
    disposition = _canonical_choice(
        data.get("disposition"),
        (CONFIG.get("disposition_multipliers") or {}).keys(),
        "disposition",
        "standard",
    ).lower()

    caps = CONFIG.get("level_caps", {"min": 1, "max": 20})
    minimum, maximum = int(caps.get("min", 1)), int(caps.get("max", 20))
    try:
        party_level = int(str(data.get("party_level") or 5).strip())
    except (TypeError, ValueError) as exc:
        raise GenerationInputError("Party level must be a whole number.") from exc
    if not minimum <= party_level <= maximum:
        raise GenerationInputError(
            f"Party level must be between {minimum} and {maximum}."
        )

    shop_name = str(data.get("shop_name") or "").strip()
    if len(shop_name) > 100 or any(ord(char) < 32 for char in shop_name):
        raise GenerationInputError(
            "Shop name must be 100 characters or fewer and cannot contain control characters."
        )
    return shop_type, shop_size, disposition, shop_name, party_level


def _unique_items(items):
    seen, output = set(), []
    for item in items or []:
        key = (
            (item.get("name") or "").strip(),
            (item.get("price") or item.get("price_text") or "").strip(),
            (item.get("rarity") or "").strip(),
            int((item.get("level") or 0) or 0),
        )
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def _is_magic_equipment(item: dict) -> bool:
    category = str(item.get("category") or "").strip().lower()
    source_table = str(item.get("source_table") or "").strip().lower()
    return bool(item.get("is_magic_countable")) or category in {
        "runed weapon",
        "runed armor",
        "runed shield",
    } or source_table.startswith("specific_magic_")


def summarize_inventory(lists: Mapping) -> dict:
    """Build mutually exclusive GM summary totals from persisted inventory lists."""
    normalized = inventory_lists(lists)
    mundane_items = normalized["mundane_items"]
    material_items = normalized["material_items"]
    armor_items = normalized["armor_items"]
    weapon_items = normalized["weapon_items"]
    magic_items = normalized["magic_items"]
    formula_items = normalized["formula_items"]

    magical_armor = _unique_items([item for item in armor_items if _is_magic_equipment(item)])
    magical_weapons = _unique_items([item for item in weapon_items if _is_magic_equipment(item)])
    armor_nonmagic = [item for item in armor_items if item not in magical_armor]
    weapons_nonmagic = [item for item in weapon_items if item not in magical_weapons]
    all_magic_items = _unique_items(magic_items + magical_armor + magical_weapons)

    picked = {
        "mundane": len(_unique_items(mundane_items)),
        "materials": len(_unique_items(material_items)),
        "armor": len(_unique_items(armor_nonmagic)),
        "weapons": len(_unique_items(weapons_nonmagic)),
        "magic": len(all_magic_items),
        "formulas": len(formula_items),
        "critical_mundane": count_critical(mundane_items),
        "critical_materials": count_critical(material_items),
        "critical_armor_shield": count_critical(armor_nonmagic),
        "critical_weapons": count_critical(weapons_nonmagic),
        "critical_magic": count_critical(all_magic_items),
    }
    picked["critical"] = sum(
        picked[key]
        for key in (
            "critical_mundane",
            "critical_materials",
            "critical_armor_shield",
            "critical_weapons",
            "critical_magic",
        )
    )
    counts = rarity_counts(flattened_inventory(normalized))
    return {"picked": picked, "counts": counts, "reproduction_warning": ""}


def generate_shop_snapshot(df: pd.DataFrame, submitted: Mapping) -> dict:
    """Validate settings, run every selector, and return a persistent snapshot."""
    effective_data = dict(submitted)
    restored = parse_reproduction_key(effective_data.get("seed"))
    source_fingerprint = ""
    if restored:
        restored = dict(restored)
        source_fingerprint = str(restored.pop("_generation_fingerprint", ""))
        effective_data.update(restored)

    shop_type, shop_size, disposition, shop_name, party_level = (
        validate_generation_inputs(effective_data, df)
    )
    generation_seed = normalize_seed(effective_data.get("seed"))
    current_fingerprint = generation_fingerprint()

    reproduction_warning = ""
    if restored and not source_fingerprint:
        reproduction_warning = (
            "This older reproduction key does not identify its catalog and generator build. "
            "The settings were restored, but exact inventory cannot be guaranteed."
        )
    elif source_fingerprint and source_fingerprint != current_fingerprint:
        reproduction_warning = (
            "This reproduction key was created with a different catalog or generator build. "
            "The settings were restored, but the inventory may differ from the original."
        )
    reproduction_key = create_reproduction_key(
        seed=generation_seed,
        shop_type=shop_type,
        shop_size=shop_size,
        disposition=disposition,
        party_level=party_level,
        fingerprint=current_fingerprint,
    )

    with generation_rng(generation_seed):
        mundane_result = select_mundane_items(
            df, shop_type, party_level, shop_size, disposition
        )
        armor_basic = select_armor_items(
            df, shop_type, party_level, shop_size, disposition
        )
        weapons_result = select_weapons_items(
            df, shop_type, party_level, shop_size, disposition
        )
        armor_magic = select_specific_magic_armor(
            df, shop_type, party_level, shop_size, disposition
        )
        weapon_magic = select_specific_magic_weapons(
            df, shop_type, party_level, shop_size, disposition
        )
        magic_basic = select_magic_items(
            df, shop_type, party_level, shop_size, disposition
        )
        material_result = select_materials(
            df, shop_type, party_level, shop_size, disposition
        )
        result_formulas = select_formulas(
            df, shop_type, party_level, shop_size, disposition
        )
        spellbook_result = select_spellbooks(
            df=df,
            shop_type=shop_type,
            party_level=party_level,
            shop_size=shop_size,
            disposition=disposition,
        )

    material_items = material_result.get("items") or []
    mundane_items = mundane_result.get("items") or []
    magic_armor = [dict(item, is_magic_countable=True) for item in (armor_magic.get("items") or [])]
    magic_weapons = [dict(item, is_magic_countable=True) for item in (weapon_magic.get("items") or [])]
    armor_items = (armor_basic.get("items") or []) + magic_armor
    weapon_items = (weapons_result.get("items") or []) + magic_weapons
    magic_items = (magic_basic.get("items") or []) + (spellbook_result.get("items") or [])

    lists = {
        "mundane_items": mundane_items,
        "material_items": material_items,
        "armor_items": armor_items,
        "weapon_items": weapon_items,
        "magic_items": magic_items,
        "formula_items": result_formulas.get("items", []),
    }
    summary = summarize_inventory(lists)
    summary["reproduction_warning"] = reproduction_warning
    magic_window = magic_basic.get("window") if isinstance(magic_basic, dict) else None

    return {
        "shop": {
            "shop_name": shop_name,
            "shop_type": shop_type,
            "shop_size": shop_size,
            "disposition": disposition,
            "party_level": party_level,
            "seed": generation_seed,
            "reproduction_key": reproduction_key,
            "generation_fingerprint": current_fingerprint,
            "window": magic_window,
        },
        "lists": lists,
        "summary": summary,
    }
