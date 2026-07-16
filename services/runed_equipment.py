"""Apply selected runes to equipment and compose the resulting display names."""

from __future__ import annotations

import random
import re
from typing import List

import pandas as pd

from services.catalog_order import canonicalize_frame
from services.money import format_gp
from services.rune_selection import (
    armor_fundamental_candidates as _fundamental_candidates_armor,
    armor_fundamental_property_candidates as _armor_fundamental_property_candidates,
    armor_property_candidates as _property_candidates_armor,
    format_fundamental_pair_label as _format_fundamental_pair_label,
    format_potency_label as _format_potency_label,
    is_shield as _is_shield,
    is_weapon_fundamental_property as _is_weapon_fundamental_property,
    pick_best_fundamental_property as _pick_best_fundamental_property,
    resolve_fundamental_property_rate as _resolve_fundamental_property_rate,
    resolve_rarity_weights as _resolve_rarity_weights,
    shield_property_candidates as _shield_property_candidates,
    weapon_fundamental_candidates as _fundamental_candidates,
    weapon_fundamental_property_candidates as _weapon_fundamental_property_candidates,
    weapon_property_candidates as _property_candidates,
    weighted_pick_by_rarity as _weighted_pick_by_rarity,
    weighted_pick_fundamental as _weighted_pick_fundamental,
)
from services.settings import CONFIG
from services.utils import bump_rarity, parse_potency_rank, to_gp


_MAT_LABEL_RX = re.compile(r"\(([^)]+)\)\s*$")
_RUNE_PREFIX_RX = re.compile(r"^\s*rune\s*[:\-]", re.IGNORECASE)


def format_rune_display_name(name: str) -> str:
    """Prefix a standalone rune name unless it is already labeled."""
    base = str(name or "").strip()
    if not base or _RUNE_PREFIX_RX.match(base):
        return base
    return f"Rune: {base}"


def extract_material_label_from_name(name: str) -> str | None:
    match = _MAT_LABEL_RX.search(str(name or "").strip())
    return match.group(1).strip() if match else None


def _adjustment_labels(item: dict) -> list[str]:
    labels = list(item.get("_adj_labels") or [])
    if labels:
        return labels
    for token in str(item.get("tags", "")).split(","):
        token = token.strip()
        if token.lower().startswith("adjustment:"):
            labels.append(token.split(":", 1)[1].strip())
    return labels


def _unique_property_labels(item: dict) -> list[str]:
    labels = [
        label.strip()
        for label in (item.get("_rune_prop_labels") or [])
        if isinstance(label, str) and label.strip()
    ]
    unique = []
    seen = set()
    for label in labels:
        key = label.lower()
        if key not in seen:
            seen.add(key)
            unique.append(label)
    return unique


def compose_weapon_name(item: dict) -> str:
    base = str(item.get("_base_name") or item.get("base_name") or item.get("name") or "").strip()
    parts = []
    fundamental = str(item.get("_rune_fund_label") or "").strip()
    if fundamental:
        parts.append(fundamental)
    parts.extend(_unique_property_labels(item))
    material = str(item.get("_mat_label") or "").strip()
    if material:
        parts.append(material)
    parts.extend(label for label in _adjustment_labels(item) if label)
    return re.sub(r"\s+", " ", " ".join([*parts, base]).strip())


def compose_armor_name(item: dict) -> str:
    base = str(item.get("_base_name") or item.get("name") or "").strip()
    parts = []
    fundamental = str(item.get("_rune_fund_label") or "").strip()
    if fundamental:
        parts.append(fundamental)
    parts.extend(_unique_property_labels(item))
    material = str(item.get("_mat_label") or "").strip()
    if material:
        parts.append(material)
    parts.extend(label for label in _adjustment_labels(item) if label)
    return re.sub(r"\s+", " ", " ".join([*parts, base]).strip())


def _format_price(gp_value: float | None) -> str:
    return format_gp(gp_value)


def apply_weapon_runes(
    weapon: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Apply fundamentals/properties with probability knobs and constraints.
    Store labels for name composition; do not mutate the weapon name here.
    """
    fused = dict(weapon)
    fused.setdefault("_base_name", (weapon.get("name") or "").strip())

    # Early out if no runes table
    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        return fused

    # Normalize columns we rely on
    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()

    # Keep only rune rows if source_table exists
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        return fused

    # Config
    rcfg = (rune_cfg or {}) if rune_cfg is not None else (CONFIG.get("runes", {}) or {})
    fund_cfg = (rcfg.get("fundamental") or {}) if isinstance(rcfg, dict) else {}
    fund_rate = float(fund_cfg.get("apply_rate", 1.0))   # default: try fund always
    pair_rate = _resolve_fundamental_property_rate(rcfg)
    prop_cfg = (rcfg.get("property") or {}) if isinstance(rcfg, dict) else {}
    prop_rate = float(prop_cfg.get("apply_rate", 0.30))
    per_slot  = float(prop_cfg.get("per_slot_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=(rcfg.get("rarity_weights") if isinstance(rcfg, dict) else None),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )
    target_level = player_level if rcfg.get("_prefer_higher_level") else None


    # Base state from current weapon row
    weapon_level = int(fused.get("level") or 0)
    base_rarity  = (fused.get("rarity") or "Common").strip().title()
    base_gp      = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0

    new_gp = base_gp
    new_rarity = base_rarity
    chosen: list[dict] = []

    # --- FUNDAMENTAL (potency first, then optional property pairing) ---
    potency = 0
    potency_rune = None
    if rng.random() < fund_rate:
        fund_cands = _fundamental_candidates(all_runes, weapon_level, player_level)
        potency_cands = [
            r
            for r in fund_cands
            if parse_potency_rank(r.get("name")) >= 1 and not _is_weapon_fundamental_property(r)
        ]
        potency_rune = _weighted_pick_fundamental(potency_cands, rng, rcfg)
        if potency_rune:
            chosen.append(potency_rune)
            new_gp += (to_gp(potency_rune.get("price_text")) or 0.0)
            new_rarity = bump_rarity(new_rarity, (potency_rune.get("rarity") or "Common"))

            potency = parse_potency_rank(potency_rune.get("name"))
            fused["_rune_fund_label"] = _format_potency_label(potency_rune)

            if potency > 0 and rng.random() < pair_rate:
                prop_cands = _weapon_fundamental_property_candidates(
                    all_runes,
                    potency_rank=potency,
                    party_level=player_level,
                )
                prop_rune = _pick_best_fundamental_property(prop_cands, potency, rng)
                if prop_rune:
                    chosen.append(prop_rune)
                    new_gp += (to_gp(prop_rune.get("price_text")) or 0.0)
                    new_rarity = bump_rarity(new_rarity, (prop_rune.get("rarity") or "Common"))
                    fused["_rune_fund_label"] = _format_fundamental_pair_label(
                        potency_rune, prop_rune
                    )

    # --- PROPERTIES (probabilistic gates per rules) ---
    prop_labels: List[str] = []
    if potency_rune:
        if potency > 0 and rng.random() < prop_rate:
            prop_cands = _property_candidates(all_runes, player_level, fused)
            picked_names = set()
            slots_taken = 0
            for _ in range(potency):
                if rng.random() >= per_slot:
                    continue
                pool = [r for r in prop_cands if r.get("name") not in picked_names]
                if not pool:
                    break
                r = _weighted_pick_by_rarity(
                    pool, rng, rarity_weights, target_level=target_level
                )
                if r is None:
                    break
                picked_names.add(r.get("name"))
                chosen.append(r)
                new_gp += (to_gp(r.get("price_text")) or 0.0)
                new_rarity = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
                prop_labels.append(str(r.get("name", "")).strip())
                slots_taken += 1
                if slots_taken >= potency:
                    break

            # Store property labels ONCE, after the loop finishes
            if prop_labels:
                fused["_rune_prop_labels"] = prop_labels

    # LEVEL = max(base, any chosen rune levels)
    try:
        base_lvl = int(fused.get("level") or 0)
    except Exception:
        base_lvl = 0
    try:
        rune_levels = [int(r.get("level") or 0) for r in (chosen or [])]
    except Exception:
        rune_levels = []
    if rune_levels:
        fused["level"] = max([base_lvl, *rune_levels])
    else:
        fused["level"] = base_lvl

    if chosen:  # at least one rune (fundamental or property)
        fused["category"] = "Runed Weapon"
        fused["is_magic_countable"] = True  # used by the summary counts
    else:
        fused["is_magic_countable"] = False

    # Save back
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused


def apply_armor_runes(
    armor: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Armor version of rune application. Mirrors weapons:
      - choose fundamental (potency/resilient) with weights / caps
      - optionally choose property runes
      - bump price/rarity
      - bump level to max(base, runes)
      - set category = 'Runed Armor' and is_magic_countable=True when any rune lands
      - stash labels for final display (if you later want to compose armor names)
    """
    fused = dict(armor)
    fused.setdefault("_base_name", (armor.get("name") or "").strip())

    # NEW: do not apply armor runes to shields
    if _is_shield(fused):
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    # normalize
    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    rcfg = (rune_cfg or {}) if rune_cfg is not None else (CONFIG.get("runes", {}) or {})
    fund_cfg = (rcfg.get("fundamental") or {}) if isinstance(rcfg, dict) else {}
    fund_rate = float(fund_cfg.get("apply_rate", 1.0))
    pair_rate = _resolve_fundamental_property_rate(rcfg)
    prop_cfg = (rcfg.get("property") or {}) if isinstance(rcfg, dict) else {}
    prop_rate = float(prop_cfg.get("apply_rate", 0.30))
    per_slot  = float(prop_cfg.get("per_slot_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=(rcfg.get("rarity_weights") if isinstance(rcfg, dict) else None),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )
    target_level = player_level if rcfg.get("_prefer_higher_level") else None

    armor_level = int(fused.get("level") or 0)
    base_rarity = (fused.get("rarity") or "Common").strip().title()
    base_gp     = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0

    new_gp = base_gp
    new_rarity = base_rarity
    chosen: list[dict] = []

    # FUNDAMENTAL
    potency = 0
    potency_rune = None
    if rng.random() < fund_rate:
        fund_cands  = _fundamental_candidates_armor(all_runes, armor_level, player_level)
        potency_cands = [r for r in fund_cands if parse_potency_rank(r.get("name")) >= 1]
        potency_rune = _weighted_pick_fundamental(potency_cands, rng, rcfg)
        if potency_rune:
            chosen.append(potency_rune)
            new_gp     += (to_gp(potency_rune.get("price_text")) or 0.0)
            new_rarity  = bump_rarity(new_rarity, (potency_rune.get("rarity") or "Common"))

            potency = parse_potency_rank(potency_rune.get("name"))  # 1..3
            fused["_rune_fund_label"] = _format_potency_label(potency_rune)

            if potency > 0 and rng.random() < pair_rate:
                prop_cands = _armor_fundamental_property_candidates(
                    all_runes,
                    potency_rank=potency,
                    party_level=player_level,
                )
                prop_rune = _pick_best_fundamental_property(prop_cands, potency, rng)
                if prop_rune:
                    chosen.append(prop_rune)
                    new_gp     += (to_gp(prop_rune.get("price_text")) or 0.0)
                    new_rarity  = bump_rarity(new_rarity, (prop_rune.get("rarity") or "Common"))
                    fused["_rune_fund_label"] = _format_fundamental_pair_label(
                        potency_rune, prop_rune
                    )

    prop_labels: List[str] = []
    if potency_rune:
        # PROPERTIES
        if potency > 0 and rng.random() < prop_rate:
            prop_cands = _property_candidates_armor(all_runes, player_level, fused)
            picked_names = set()
            slots_taken = 0
            for _ in range(potency):
                if rng.random() >= per_slot:
                    continue
                pool = [r for r in prop_cands if r.get("name") not in picked_names]
                if not pool:
                    break
                r = _weighted_pick_by_rarity(
                    pool, rng, rarity_weights, target_level=target_level
                )
                if r is None:
                    break
                picked_names.add(r.get("name"))
                chosen.append(r)
                new_gp     += (to_gp(r.get("price_text")) or 0.0)
                new_rarity  = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
                prop_labels.append(str(r.get("name", "")).strip())
                slots_taken += 1
                if slots_taken >= potency:
                    break
    if prop_labels:
        fused["_rune_prop_labels"] = prop_labels

    # LEVEL bump
    try:
        base_lvl = int(fused.get("level") or 0)
    except Exception:
        base_lvl = 0
    try:
        rune_levels = [int(r.get("level") or 0) for r in (chosen or [])]
    except Exception:
        rune_levels = []
    fused["level"] = max([base_lvl, *rune_levels]) if rune_levels else base_lvl

    # CATEGORY / flags
    if chosen:
        fused["category"] = "Runed Armor"
        fused["is_magic_countable"] = True
        prev = (str(fused.get("tags", "")) or "").strip()
        fused["tags"] = ", ".join(t for t in [prev, "Runed"] if t).strip(", ")
    else:
        fused["is_magic_countable"] = False

    # Save back
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused


def apply_shield_runes(
    armor_row: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Shields only get a single property rune (no fundamentals).
    - Candidate level <= player_level + 1
    - Update price/rarity/level
    - Category = 'Runed Shield'
    - is_magic_countable = True
    """
    fused = dict(armor_row)
    fused.setdefault("_base_name", (armor_row.get("name") or "").strip())

    # Only handle shields here; non-shields pass through unchanged
    if not _is_shield(fused):
        return fused

    # Load/normalize runes table
    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    # Config: prefer shield_runes > armor_runes > runes
    rcfg = rune_cfg or CONFIG.get("shield_runes") or CONFIG.get("armor_runes") or CONFIG.get("runes") or {}
    if not isinstance(rcfg, dict):
        rcfg = {}
    prop_rate = float((rcfg.get("property") or {}).get("apply_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=rcfg.get("rarity_weights"),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )
    target_level = player_level if rcfg.get("_prefer_higher_level") else None

    # Base state
    base_rarity = (fused.get("rarity") or "Common").strip().title()
    base_gp     = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0
    new_gp      = base_gp
    new_rarity  = base_rarity

    # Single property rune gate
    chosen: list[dict] = []
    if rng.random() < prop_rate:
        pool = _shield_property_candidates(all_runes, player_level, fused)
        if pool:
            r = _weighted_pick_by_rarity(
                pool, rng, rarity_weights, target_level=target_level
            )
            if r is None:
                r = pool[rng.randint(0, len(pool) - 1)]
            chosen.append(r)
            new_gp     += (to_gp(r.get("price_text")) or 0.0)
            new_rarity  = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
            # label for composer
            fused["_rune_prop_labels"] = [str(r.get("name", "")).strip()]

            # level bump = max(base, rune level)
            try:
                base_lvl = int(fused.get("level") or 0)
            except Exception:
                base_lvl = 0
            rune_lvl = int(r.get("level") or 0)
            fused["level"] = max(base_lvl, rune_lvl)

            # category / flag / tags
            fused["category"] = "Runed Shield"
            fused["is_magic_countable"] = True
            prev = (str(fused.get("tags", "")) or "").strip()
            fused["tags"] = ", ".join(t for t in [prev, "Runed"] if t).strip(", ")
        else:
            fused["is_magic_countable"] = False
    else:
        fused["is_magic_countable"] = False

    # Finalize
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused
