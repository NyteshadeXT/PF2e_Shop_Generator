import random
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

from services.logic import apply_armor_runes, apply_shield_runes, apply_weapon_runes


def _build_df(rows):
    return pd.DataFrame(rows)


def test_weapon_prerequisites_block_incompatible_property():
    weapon = {
        "name": "Longbow",
        "type": "Martial Ranged",
        "tags": "Deadly d10; Volley 30ft",
        "damage_type": "Piercing",
        "rarity": "Common",
        "level": 5,
        "price_text": "100 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Weapon Potency +1",
                "Type": "Weapon Fundamental Runes",
                "level": 2,
                "price_text": "35 gp",
                "rarity": "Common",
                "source_table": "runes",
            },
            {
                "name": "Bloodthirsty",
                "Type": "Weapon Property Runes",
                "Prerequisite": "Piercing; Slashing",
                "level": 8,
                "price_text": "500 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
        ]
    )
    cfg = {
        "fundamental": {"apply_rate": 1.0, "potency_weights": {"1": 1.0}},
        "property": {"apply_rate": 1.0, "per_slot_rate": 1.0},
    }

    fused = apply_weapon_runes(
        weapon,
        player_level=10,
        runes_df=runes_df,
        rng=random.Random(1),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Bloodthirsty" not in rune_names


def test_armor_prerequisites_block_incompatible_property():
    armor = {
        "name": "Full Plate",
        "type": "Heavy",
        "tags": "Bulwark",
        "rarity": "Common",
        "level": 8,
        "price_text": "300 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Armor Potency +1",
                "Type": "Armor Fundamental Runes",
                "level": 5,
                "price_text": "160 gp",
                "rarity": "Common",
                "source_table": "runes",
            },
            {
                "name": "Invisibility",
                "Type": "Armor Property Runes",
                "Prerequisite": "Light",
                "level": 10,
                "price_text": "800 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
        ]
    )
    cfg = {
        "fundamental": {"apply_rate": 1.0, "potency_weights": {"1": 1.0}},
        "property": {"apply_rate": 1.0, "per_slot_rate": 1.0},
    }

    fused = apply_armor_runes(
        armor,
        player_level=12,
        runes_df=runes_df,
        rng=random.Random(2),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Invisibility" not in rune_names


def test_shield_prerequisites_filter_non_shield_runes():
    shield = {
        "name": "Sturdy Shield",
        "type": "Shield",
        "subtype": "shield",
        "rarity": "Common",
        "level": 6,
        "price_text": "250 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Aggressive",
                "Type": "Shield Property Runes",
                "Prerequisite": "Shield",
                "level": 6,
                "price_text": "340 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
            {
                "name": "Footwear Rune",
                "Type": "Shield Property Runes",
                "Prerequisite": "Footwear",
                "level": 4,
                "price_text": "80 gp",
                "rarity": "Common",
                "source_table": "runes",
            },
        ]
    )
    cfg = {"property": {"apply_rate": 1.0}}

    fused = apply_shield_runes(
        shield,
        player_level=8,
        runes_df=runes_df,
        rng=random.Random(3),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Footwear Rune" not in rune_names
    assert "Aggressive" in rune_names


def test_weapon_properties_require_weapon_property_runes_type():
    weapon = {
        "name": "Longsword",
        "type": "Martial Melee",
        "rarity": "Common",
        "level": 8,
        "price_text": "100 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Weapon Potency +1",
                "Type": "Weapon Fundamental Runes",
                "level": 2,
                "price_text": "35 gp",
                "rarity": "Common",
                "source_table": "runes",
            },
            {
                "name": "Wrongly Typed Rune",
                "Type": "Armor Property Runes",
                "level": 8,
                "price_text": "500 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
        ]
    )
    cfg = {
        "fundamental": {"apply_rate": 1.0, "potency_weights": {"1": 1.0}},
        "property": {"apply_rate": 1.0, "per_slot_rate": 1.0},
    }

    fused = apply_weapon_runes(
        weapon,
        player_level=10,
        runes_df=runes_df,
        rng=random.Random(4),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Wrongly Typed Rune" not in rune_names


def test_armor_properties_require_armor_property_runes_type():
    armor = {
        "name": "Chain Mail",
        "type": "Medium",
        "rarity": "Common",
        "level": 8,
        "price_text": "100 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Armor Potency +1",
                "Type": "Armor Fundamental Runes",
                "level": 5,
                "price_text": "160 gp",
                "rarity": "Common",
                "source_table": "runes",
            },
            {
                "name": "Wrongly Typed Rune",
                "Type": "Weapon Property Runes",
                "level": 8,
                "price_text": "500 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
        ]
    )
    cfg = {
        "fundamental": {"apply_rate": 1.0, "potency_weights": {"1": 1.0}},
        "property": {"apply_rate": 1.0, "per_slot_rate": 1.0},
    }

    fused = apply_armor_runes(
        armor,
        player_level=10,
        runes_df=runes_df,
        rng=random.Random(5),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Wrongly Typed Rune" not in rune_names


def test_shield_properties_require_shield_property_runes_type():
    shield = {
        "name": "Steel Shield",
        "type": "Shield",
        "subtype": "shield",
        "rarity": "Common",
        "level": 6,
        "price_text": "20 gp",
    }
    runes_df = _build_df(
        [
            {
                "name": "Wrongly Typed Rune",
                "Type": "Weapon Property Runes",
                "Prerequisite": "Shield",
                "level": 6,
                "price_text": "340 gp",
                "rarity": "Uncommon",
                "source_table": "runes",
            },
        ]
    )
    cfg = {"property": {"apply_rate": 1.0}}

    fused = apply_shield_runes(
        shield,
        player_level=8,
        runes_df=runes_df,
        rng=random.Random(6),
        rune_cfg=cfg,
    )

    rune_names = {r["name"] for r in fused.get("runes", [])}
    assert "Wrongly Typed Rune" not in rune_names