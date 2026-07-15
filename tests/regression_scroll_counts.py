import copy
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

import services.logic as logic
from services.logic import CONFIG, _enrich_spell_scrolls, select_magic_items


def test_scroll_counts_add_to_magic_output_when_configured():
    original_counts = copy.deepcopy(CONFIG.get("counts"))
    original_counts_by_shop = copy.deepcopy(CONFIG.get("counts_by_shop"))
    try:
        CONFIG["counts_by_shop"] = {}
        CONFIG["counts"] = {
            "medium": {
                "magic": [0, 0],
                "scrolls": [2, 2],
            }
        }

        df = pd.DataFrame(
            [
                {
                    "name": "Spell scroll (1st level)",
                    "source_table": "scrolls",
                    "shop_type": "Blacksmith",
                    "rarity": "Common",
                    "price_text": "4 gp",
                    "level": 3,
                    "stock_flag": 0,
                }
            ]
        )

        result = select_magic_items(
            df=df,
            shop_type="Blacksmith",
            party_level=5,
            shop_size="medium",
            disposition="fair",
        )

        items = result.get("items") or []
        total_qty = sum(int(it.get("quantity") or 1) for it in items)
        assert total_qty >= 2
        assert all(str(it.get("source_table", "")).lower() == "scrolls" for it in items)
    finally:
        CONFIG["counts"] = original_counts
        CONFIG["counts_by_shop"] = original_counts_by_shop


def test_scroll_counts_do_not_error_when_shop_has_no_scroll_rows():
    original_counts = copy.deepcopy(CONFIG.get("counts"))
    original_counts_by_shop = copy.deepcopy(CONFIG.get("counts_by_shop"))
    try:
        CONFIG["counts_by_shop"] = {}
        CONFIG["counts"] = {
            "medium": {
                "magic": [1, 1],
                "scrolls": [2, 2],
            }
        }

        df = pd.DataFrame(
            [
                {
                    "name": "Cloak of Elvenkind",
                    "source_table": "worn_items",
                    "shop_type": "Blacksmith",
                    "rarity": "Common",
                    "price_text": "30 gp",
                    "level": 3,
                    "stock_flag": 0,
                }
            ]
        )

        result = select_magic_items(
            df=df,
            shop_type="Blacksmith",
            party_level=5,
            shop_size="medium",
            disposition="fair",
        )

        items = result.get("items") or []
        assert items
        assert all(str(it.get("source_table", "")).lower() != "scrolls" for it in items)
    finally:
        CONFIG["counts"] = original_counts
        CONFIG["counts_by_shop"] = original_counts_by_shop


def test_additive_scroll_picks_are_spell_enriched():
    original_counts = copy.deepcopy(CONFIG.get("counts"))
    original_counts_by_shop = copy.deepcopy(CONFIG.get("counts_by_shop"))
    original_ensure_spells_cache = logic._ensure_spells_cache
    try:
        CONFIG["counts_by_shop"] = {}
        CONFIG["counts"] = {
            "medium": {
                "magic": [0, 0],
                "scrolls": [1, 1],
            }
        }

        spells_df = pd.DataFrame([{"Name": "Magic Missile", "Rank": 1, "Rarity": "Common", "Source": "PC1"}])
        logic._ensure_spells_cache = lambda: (spells_df, {1: spells_df})

        df = pd.DataFrame(
            [
                {
                    "name": "Spell scroll (1st level)",
                    "source_table": "scrolls",
                    "shop_type": "Blacksmith",
                    "rarity": "Common",
                    "price_text": "4 gp",
                    "level": 3,
                    "stock_flag": 0,
                }
            ]
        )

        result = select_magic_items(
            df=df,
            shop_type="Blacksmith",
            party_level=5,
            shop_size="medium",
            disposition="fair",
        )

        items = result.get("items") or []
        assert items
        assert any(" - Magic Missile" in str(it.get("name", "")) for it in items)
        assert any((it.get("spell") or {}).get("name") == "Magic Missile" for it in items)
        assert any(str(it.get("Source", "")).strip() == "PC1" for it in items)
    finally:
        logic._ensure_spells_cache = original_ensure_spells_cache
        CONFIG["counts"] = original_counts
        CONFIG["counts_by_shop"] = original_counts_by_shop


def test_additive_scroll_enrichment_handles_non_exact_scroll_name_format():
    original_counts = copy.deepcopy(CONFIG.get("counts"))
    original_counts_by_shop = copy.deepcopy(CONFIG.get("counts_by_shop"))
    original_ensure_spells_cache = logic._ensure_spells_cache
    try:
        CONFIG["counts_by_shop"] = {}
        CONFIG["counts"] = {
            "medium": {
                "magic": [0, 0],
                "scrolls": [1, 1],
            }
        }

        spells_df = pd.DataFrame([{"Name": "Show The Way", "Rank": 3, "Rarity": "Common"}])
        logic._ensure_spells_cache = lambda: (spells_df, {3: spells_df})

        df = pd.DataFrame(
            [
                {
                    "name": "Spell scroll (3rd level) [Arcane]",
                    "source_table": "scrolls",
                    "shop_type": "Blacksmith",
                    "rarity": "Common",
                    "price_text": "30 gp",
                    "level": 6,
                    "stock_flag": 0,
                }
            ]
        )

        result = select_magic_items(
            df=df,
            shop_type="Blacksmith",
            party_level=6,
            shop_size="medium",
            disposition="fair",
        )

        items = result.get("items") or []
        assert items
        assert any("Show The Way" in str(it.get("name", "")) for it in items)
    finally:
        logic._ensure_spells_cache = original_ensure_spells_cache
        CONFIG["counts"] = original_counts
        CONFIG["counts_by_shop"] = original_counts_by_shop


def test_additive_scroll_merge_does_not_double_enrich_existing_scroll_name():
    original_counts = copy.deepcopy(CONFIG.get("counts"))
    original_counts_by_shop = copy.deepcopy(CONFIG.get("counts_by_shop"))
    original_ensure_spells_cache = logic._ensure_spells_cache
    try:
        CONFIG["counts_by_shop"] = {}
        CONFIG["counts"] = {
            "medium": {
                "magic": [0, 0],
                "scrolls": [1, 1],
            }
        }

        spells_df = pd.DataFrame([{"Name": "Magic Missile", "Rank": 1, "Rarity": "Common"}])
        logic._ensure_spells_cache = lambda: (spells_df, {1: spells_df})

        df = pd.DataFrame(
            [
                {
                    "name": "Spell scroll (1st level) - Existing Spell",
                    "source_table": "scrolls",
                    "shop_type": "Blacksmith",
                    "rarity": "Common",
                    "price_text": "4 gp",
                    "level": 3,
                    "stock_flag": 0,
                }
            ]
        )

        result = select_magic_items(
            df=df,
            shop_type="Blacksmith",
            party_level=5,
            shop_size="medium",
            disposition="fair",
        )

        items = result.get("items") or []
        assert items
        assert all(" - Existing Spell - " not in str(it.get("name", "")) for it in items)
    finally:
        logic._ensure_spells_cache = original_ensure_spells_cache
        CONFIG["counts"] = original_counts
        CONFIG["counts_by_shop"] = original_counts_by_shop


def test_scroll_quantity_enrichment_rolls_per_item_and_merges_duplicates():
    original_ensure_spells_cache = logic._ensure_spells_cache
    try:
        spells_df = pd.DataFrame([{"Name": "Show The Way", "Rank": 4, "Rarity": "Common"}])
        logic._ensure_spells_cache = lambda: (spells_df, {4: spells_df})

        items = [
            {
                "name": "Spell scroll (4th level)",
                "quantity": 3,
                "rarity": "Common",
                "price": "70 gp",
                "price_text": "70 gp",
                "level": 8,
                "critical": False,
            }
        ]

        enriched = _enrich_spell_scrolls(items)
        assert len(enriched) == 1
        assert enriched[0].get("quantity") == 3
        assert "Show The Way" in str(enriched[0].get("name", ""))
    finally:
        logic._ensure_spells_cache = original_ensure_spells_cache
