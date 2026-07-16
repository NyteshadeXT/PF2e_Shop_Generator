"""Spell-scroll and wand enrichment for generated merchant inventory."""

from __future__ import annotations

from collections.abc import Callable
import logging
import re

import pandas as pd

from services.catalog_order import canonicalize_frame
from services.db import load_spells
from services.money import cp_to_gp, format_gp, gp_to_cp, multiply_cp
from services.randomness import get_rng
from services.settings import CONFIG
from services.utils import to_gp


logger = logging.getLogger(__name__)

_SCROLL_RE = re.compile(
    r"spell\s*scroll\s*\((\d+)(?:st|nd|rd|th)\s*level\)", re.IGNORECASE
)
_WAND_LEVEL_RE = re.compile(
    r"(\d+)(?:st|nd|rd|th)[-\s]*(?:level|rank) spell", re.IGNORECASE
)

_SPELLS_DF_CACHE: pd.DataFrame | None = None
_SPELLS_BY_RANK_CACHE: dict[int, pd.DataFrame] | None = None
_SPELLS_CACHE_SIGNATURE = None

SpellCacheLoader = Callable[[], tuple[pd.DataFrame, dict[int, pd.DataFrame]]]


def load_spell_cache() -> tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    """Load and index the shared spell reference table by spell rank."""
    global _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE, _SPELLS_CACHE_SIGNATURE

    spells_df = pd.DataFrame()
    try:
        source = load_spells()
        signature = source.attrs.get("reference_signature")
        if (
            _SPELLS_DF_CACHE is not None
            and _SPELLS_BY_RANK_CACHE is not None
            and _SPELLS_CACHE_SIGNATURE == signature
        ):
            return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE
        spells_df = source.rename(
            columns={
                "name": "Name",
                "rank": "Rank",
                "rarity": "Rarity",
                "source": "Source",
            }
        )[["Name", "Rank", "Rarity", "Source"]].copy()
    except Exception:
        logger.warning("Could not load the Spells table", exc_info=True)
        signature = None

    if spells_df is None or spells_df.empty:
        _SPELLS_DF_CACHE = pd.DataFrame()
        _SPELLS_BY_RANK_CACHE = {}
        _SPELLS_CACHE_SIGNATURE = signature
        return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE

    spells_df["Name"] = spells_df["Name"].astype(str).str.strip()
    spells_df["Rank"] = (
        pd.to_numeric(spells_df["Rank"], errors="coerce").fillna(0).astype(int)
    )
    spells_df["Rarity"] = spells_df["Rarity"].astype(str).str.strip().str.title()
    spells_df["Source"] = spells_df["Source"].astype(str).str.strip()
    spells_df = canonicalize_frame(spells_df)

    by_rank = {
        int(rank): group.copy() for rank, group in spells_df.groupby("Rank")
    }
    _SPELLS_DF_CACHE = spells_df
    _SPELLS_BY_RANK_CACHE = by_rank
    _SPELLS_CACHE_SIGNATURE = signature
    return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE


def parse_scroll_level(name: str) -> int | None:
    raw = str(name or "").strip()
    base = raw.split(" - ", 1)[0].strip()
    match = _SCROLL_RE.search(base)
    return int(match.group(1)) if match else None


def parse_wand_rank(name: str, level: int | None = None) -> int | None:
    match = _WAND_LEVEL_RE.search(str(name or "").strip())
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    try:
        item_level = int(level) if level is not None else 0
    except (TypeError, ValueError):
        return None
    if item_level <= 0:
        return None
    return max(1, min(10, (item_level + 1) // 2))


def _rarity_multipliers() -> dict[str, float]:
    return {
        "Uncommon": 1.25,
        "Rare": 1.50,
        **(CONFIG.get("rarity_price_multipliers", {}) or {}),
    }


def _multiply_gp(gp_value: float, multiplier: float) -> float:
    return float(cp_to_gp(multiply_cp(gp_to_cp(gp_value), multiplier)))


def enrich_spell_scrolls(
    items: list[dict], *, spell_cache_loader: SpellCacheLoader = load_spell_cache
) -> list[dict]:
    if not items:
        return items
    spells_df, spells_by_rank = spell_cache_loader()
    if spells_df is None or spells_df.empty:
        return items

    multipliers = _rarity_multipliers()
    rng = get_rng()
    expanded = []
    for item in items:
        for _ in range(max(1, int(item.get("quantity") or 1))):
            unit = dict(item)
            unit["quantity"] = 1
            expanded.append(unit)

    enriched = []
    for item in expanded:
        name = str(item.get("name", "")).strip()
        if " - " in name and _SCROLL_RE.search(name.split(" - ", 1)[0].strip()):
            enriched.append(item)
            continue
        rank = parse_scroll_level(name)
        pool = spells_by_rank.get(rank) if rank is not None else None
        if pool is None or pool.empty:
            enriched.append(item)
            continue

        pick = pool.sample(
            n=1, replace=True, random_state=rng.randint(0, 10**9)
        ).iloc[0]
        spell_name = str(pick.get("Name", "")).strip()
        spell_rarity = str(pick.get("Rarity", "Common")).title()
        spell_source = str(pick.get("Source", "")).strip()
        base_gp = to_gp(item.get("price", ""))
        if base_gp is None:
            base_gp = to_gp(item.get("price_text", ""))
        new_gp = _multiply_gp(
            base_gp or 0.0, float(multipliers.get(spell_rarity, 1.0))
        )

        fused = dict(item)
        fused["name"] = f"{name} - {spell_name}"
        fused["price"] = format_gp(new_gp)
        fused["spell"] = {
            "name": spell_name,
            "rarity": spell_rarity,
            "rank": int(rank),
        }
        fused["aon_target"] = spell_name
        if spell_source:
            fused["Source"] = spell_source
        enriched.append(fused)

    merged: dict[tuple, dict] = {}
    for item in enriched:
        key = (
            str(item.get("name", "")).strip(),
            str(item.get("price", "")).strip(),
            str(item.get("rarity", "")).strip(),
            int(item.get("level") or 0),
            bool(item.get("critical")),
        )
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["quantity"] = 0
        merged[key]["quantity"] += int(item.get("quantity") or 1)
    return list(merged.values())


def enrich_magic_wands(
    items: list[dict], *, spell_cache_loader: SpellCacheLoader = load_spell_cache
) -> list[dict]:
    if not items:
        return items
    spells_df, spells_by_rank = spell_cache_loader()
    if spells_df is None or spells_df.empty:
        return items

    multipliers = _rarity_multipliers()
    rng = get_rng()
    enriched = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if str(item.get("type", "")).strip().lower() != "wands" or not name:
            enriched.append(item)
            continue
        rank = parse_wand_rank(name, item.get("level"))
        pool = spells_by_rank.get(rank) if rank else None
        if pool is None or pool.empty:
            enriched.append(item)
            continue

        pick = pool.sample(
            n=1, replace=True, random_state=rng.randint(0, 10**9)
        ).iloc[0]
        spell_name = str(pick.get("Name", "")).strip()
        if not spell_name:
            enriched.append(item)
            continue
        spell_rarity = str(pick.get("Rarity", "Common")).strip().title() or "Common"
        base_gp = to_gp(item.get("price", ""))
        if base_gp is None:
            base_gp = to_gp(item.get("price_text", ""))
        new_gp = _multiply_gp(
            base_gp or 0.0, float(multipliers.get(spell_rarity, 1.0))
        )

        fused = dict(item)
        fused["name"] = f"{name} - {spell_name}"
        fused["price"] = format_gp(new_gp)
        fused["spell"] = {
            "name": spell_name,
            "rarity": spell_rarity,
            "rank": int(rank),
        }
        fused["aon_target"] = spell_name
        enriched.append(fused)
    return enriched
