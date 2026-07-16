"""Rune classification, eligibility, potency caps, and weighted selection."""

from __future__ import annotations

from collections.abc import Callable
import random

import pandas as pd

from services.catalog_order import canonicalize_frame
from services.db import load_items
from services.rune_prerequisites import collect_item_context, prerequisites_match
from services.utils import parse_potency_rank


def is_shield(item: dict) -> bool:
    subtype = str(item.get("subtype") or item.get("Subtype") or "").strip().lower()
    category = str(item.get("category") or "").strip().lower()
    return "shield" in subtype or "shield" in category


def _is_shield_property(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    return rune_type == "shield property runes"


def shield_property_candidates(
    all_runes: list[dict], party_level: int, shield_row: dict
) -> list[dict]:
    maximum_level = int(party_level) + 1
    context = collect_item_context(shield_row)
    return [
        rune
        for rune in all_runes
        if _is_shield_property(rune)
        and prerequisites_match(rune, context)
        and int(rune.get("level") or 0) <= maximum_level
    ]


def _is_armor_fundamental(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    subtype = str(row.get("Subtype") or row.get("subtype") or "").strip().lower()
    name = str(row.get("name") or "").strip().lower()
    if "fundamental" in rune_type and "armor" in rune_type:
        return True
    if "fundamental" in subtype and "armor" in subtype:
        return True
    return ("armor" in rune_type or "armor" in subtype or "armor" in name) and (
        "potency" in name or "resilient" in name
    )


def _is_armor_fundamental_property(row: dict) -> bool:
    if not _is_armor_fundamental(row):
        return False
    name = str(row.get("name") or "").strip().lower()
    if "resilient" in name:
        return True
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    return all(token in rune_type for token in ("fundamental", "property", "armor"))


def _is_armor_property(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    return rune_type == "armor property runes"


def armor_potency_cap(party_level: int) -> int:
    level = int(party_level or 0)
    if level < 5:
        return 0
    if level < 11:
        return 1
    if level < 18:
        return 2
    return 3


def armor_fundamental_candidates(
    all_runes: list[dict], armor_level: int, party_level: int
) -> list[dict]:
    del armor_level  # Eligibility is based on the party cap and rune item level.
    if int(party_level) < 5:
        return []
    cap = armor_potency_cap(party_level)
    maximum_level = int(party_level) + 1
    return [
        rune
        for rune in all_runes
        if _is_armor_fundamental(rune)
        and 1 <= parse_potency_rank(rune.get("name")) <= cap
        and int(rune.get("level") or 0) <= maximum_level
    ]


def _required_potency(name: str) -> int:
    normalized = str(name or "").strip().lower()
    if "major" in normalized:
        return 3
    if "greater" in normalized:
        return 2
    return 1


def _collect_fundamental_property_candidates(
    all_runes: list[dict],
    *,
    potency_rank: int,
    party_level: int,
    cap_function: Callable[[int], int],
    predicate: Callable[[dict], bool],
) -> list[dict]:
    potency_rank = int(potency_rank or 0)
    cap = int(cap_function(party_level))
    if potency_rank <= 0 or cap <= 0:
        return []
    maximum_level = int(party_level) + 1
    ranked = []
    for rune in all_runes:
        if not predicate(rune):
            continue
        required = _required_potency(rune.get("name"))
        level = int(rune.get("level") or 0)
        if required <= potency_rank and required <= cap and level <= maximum_level:
            ranked.append((required, level, rune))
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [rune for _required, _level, rune in ranked]


def armor_fundamental_property_candidates(
    all_runes: list[dict], *, potency_rank: int, party_level: int
) -> list[dict]:
    return _collect_fundamental_property_candidates(
        all_runes,
        potency_rank=potency_rank,
        party_level=party_level,
        cap_function=armor_potency_cap,
        predicate=_is_armor_fundamental_property,
    )


def armor_property_candidates(
    all_runes: list[dict], party_level: int, armor_row: dict
) -> list[dict]:
    minimum_level, maximum_level = party_level - 3, party_level + 1
    context = collect_item_context(armor_row)
    return [
        rune
        for rune in all_runes
        if _is_armor_property(rune)
        and prerequisites_match(rune, context)
        and minimum_level <= int(rune.get("level") or 0) <= maximum_level
    ]


def weapon_potency_cap(party_level: int) -> int:
    level = int(party_level or 0)
    if level < 2:
        return 0
    if level < 10:
        return 1
    if level < 16:
        return 2
    return 3


def _is_weapon_fundamental(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    name = str(row.get("name") or "").strip().lower()
    return ("fundamental" in rune_type and "weapon" in rune_type) or (
        "potency" in name and "weapon" in name
    )


def _is_weapon_property(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    return rune_type == "weapon property runes"


def _is_weapon_fundamental_property(row: dict) -> bool:
    rune_type = str(row.get("Type") or row.get("type") or "").strip().lower()
    name = str(row.get("name") or "").strip().lower()
    return all(token in rune_type for token in ("weapon", "fundamental", "property")) or all(
        token in name for token in ("weapon", "fundamental", "property")
    )


def is_weapon_fundamental_property(row: dict) -> bool:
    """Return whether a catalog row is a weapon fundamental-property rune."""
    return _is_weapon_fundamental_property(row)


def weapon_fundamental_property_candidates(
    all_runes: list[dict], *, potency_rank: int, party_level: int
) -> list[dict]:
    return _collect_fundamental_property_candidates(
        all_runes,
        potency_rank=potency_rank,
        party_level=party_level,
        cap_function=weapon_potency_cap,
        predicate=_is_weapon_fundamental_property,
    )


def pick_best_fundamental_property(
    candidates: list[dict], potency_rank: int, rng: random.Random
) -> dict | None:
    if not candidates:
        return None
    eligible = [
        rune for rune in candidates if _required_potency(rune.get("name")) <= potency_rank
    ]
    if eligible:
        best_required = max(_required_potency(rune.get("name")) for rune in eligible)
        pool = [
            rune for rune in eligible if _required_potency(rune.get("name")) == best_required
        ]
    else:
        pool = candidates
    return pool[rng.randint(0, len(pool) - 1)]


def resolve_fundamental_property_rate(
    rune_config: dict | None, *, default: float = 0.6
) -> float:
    def numeric(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if isinstance(rune_config, dict):
        property_config = rune_config.get("fundamental property") or rune_config.get(
            "fundamental_property"
        )
        if isinstance(property_config, dict):
            rate = numeric(property_config.get("apply_rate"))
            if rate is not None:
                return rate
        fundamental_config = rune_config.get("fundamental")
        if isinstance(fundamental_config, dict):
            rate = numeric(fundamental_config.get("property_pair_rate"))
            if rate is not None:
                return rate
    return float(default)


def resolve_rarity_weights(
    primary: dict | None, fallback: dict | None = None
) -> dict[str, float]:
    merged: dict[str, float] = {}
    for source in (fallback, primary):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            merged[str(key).strip().title()] = max(weight, 0.0)
    if not merged:
        merged = {"Common": 1.0}
    if merged.get("Common", 0.0) <= 0:
        merged["Common"] = 1.0
    return merged


def weighted_pick_by_rarity(
    pool: list[dict],
    rng: random.Random,
    rarity_weights: dict[str, float],
    *,
    target_level: int | None = None,
) -> dict | None:
    if not pool:
        return None
    default_weight = float(rarity_weights.get("Common", 1.0))
    weights = []
    for rune in pool:
        rarity = str(rune.get("rarity") or "Common").strip().title()
        try:
            weight = float(rarity_weights.get(rarity, default_weight))
        except (TypeError, ValueError):
            weight = default_weight
        if target_level is not None:
            distance = abs(int(target_level) - int(rune.get("level") or 0))
            weight *= 1.0 / ((distance + 1) ** 2)
        weights.append(max(weight, 0.0))
    if not any(weight > 0 for weight in weights):
        return pool[rng.randint(0, len(pool) - 1)]
    return _roulette_pick(pool, weights, rng)


def _roulette_pick(pool: list[dict], weights: list[float], rng: random.Random) -> dict:
    target = rng.random() * sum(weights)
    accumulated = 0.0
    for rune, weight in zip(pool, weights):
        accumulated += weight
        if target <= accumulated:
            return rune
    return pool[-1]


def format_potency_label(potency_rune: dict | None) -> str:
    if not potency_rune:
        return ""
    rank = parse_potency_rank(potency_rune.get("name"))
    return f"+{rank}" if rank else str(potency_rune.get("name") or "").strip()


def format_fundamental_pair_label(
    potency_rune: dict | None, property_rune: dict
) -> str:
    potency_label = format_potency_label(potency_rune)
    property_label = str(property_rune.get("name") or "").strip()
    return " ".join(part for part in (potency_label, property_label) if part)


def weapon_fundamental_candidates(
    all_runes: list[dict], weapon_level: int, party_level: int
) -> list[dict]:
    del weapon_level
    if int(party_level) < 2:
        return []
    cap = weapon_potency_cap(party_level)
    maximum_level = int(party_level) + 1
    candidates = []
    for rune in all_runes:
        if not _is_weapon_fundamental(rune):
            continue
        rank = parse_potency_rank(rune.get("name"))
        level = int(rune.get("level") or 0)
        if rank >= 1:
            if rank <= cap and level <= maximum_level:
                candidates.append(rune)
            continue
        if _is_weapon_fundamental_property(rune) and level <= maximum_level:
            required = _required_potency(rune.get("name"))
            has_potency = any(
                _is_weapon_fundamental(other)
                and not _is_weapon_fundamental_property(other)
                and required <= parse_potency_rank(other.get("name")) <= cap
                and int(other.get("level") or 0) <= maximum_level
                for other in all_runes
            )
            if required <= cap and has_potency:
                candidates.append(rune)
    return candidates


def weighted_pick_fundamental(
    candidates: list[dict], rng: random.Random, config: dict | None
) -> dict | None:
    if not candidates:
        return None
    fundamental_config = (config or {}).get("fundamental", {}) if config else {}
    configured = (
        fundamental_config.get("potency_weights")
        if isinstance(fundamental_config, dict)
        else None
    )
    if configured:
        potency_weights = {str(key): float(value) for key, value in configured.items()}
    else:
        ranks = {
            parse_potency_rank(rune.get("name"))
            for rune in candidates
            if parse_potency_rank(rune.get("name")) > 0
        }
        potency_weights = {str(rank): float(rank) for rank in sorted(ranks)}

    target_level = None
    if config and config.get("_prefer_higher_level"):
        try:
            target_level = int(config.get("_target_level"))
        except (TypeError, ValueError):
            target_level = None
    weights = []
    for rune in candidates:
        rank = parse_potency_rank(rune.get("name"))
        weight = float(potency_weights.get(str(rank), 1.0))
        if target_level is not None:
            distance = abs(target_level - int(rune.get("level") or 0))
            weight *= 1.0 / ((distance + 1) ** 2)
        weights.append(max(weight, 0.0001))
    return _roulette_pick(candidates, weights, rng)


def weapon_property_candidates(
    all_runes: list[dict], party_level: int, weapon_row: dict
) -> list[dict]:
    minimum_level, maximum_level = party_level - 3, party_level + 1
    context = collect_item_context(weapon_row)
    candidates = [
        rune
        for rune in all_runes
        if _is_weapon_property(rune)
        and prerequisites_match(rune, context)
        and minimum_level <= int(rune.get("level") or 0) <= maximum_level
    ]
    candidates.sort(key=lambda rune: int(rune.get("level") or 0), reverse=True)
    return candidates


def load_runes_frame() -> pd.DataFrame:
    items = load_items()
    if items is None or items.empty:
        return pd.DataFrame()
    runes = items.copy()
    for column in ("source_table", "Type", "name", "rarity", "price_text"):
        if column in runes.columns:
            runes[column] = runes[column].astype(str).str.strip()
    if "level" in runes.columns:
        runes["level"] = (
            pd.to_numeric(runes["level"], errors="coerce").fillna(0).astype(int)
        )
    return canonicalize_frame(
        runes[runes.get("source_table", "").str.lower().eq("runes")]
    )
