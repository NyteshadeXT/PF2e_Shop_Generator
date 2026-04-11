import random
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.logic import (
    _fundamental_candidates,
    _load_runes_df,
    apply_armor_runes,
    apply_weapon_runes,
)


class SequenceRandom(random.Random):
    """Deterministic RNG that yields a preset sequence for random()."""

    def __init__(self, sequence, *, seed=0):
        super().__init__(seed)
        self._sequence = list(sequence)
        self._index = 0

    def random(self):
        if self._index < len(self._sequence):
            value = self._sequence[self._index]
            self._index += 1
            return float(value)
        return super().random()

def _mock_weapon_runes_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Type": "Weapon Fundamental Rune",
                "name": "Weapon Potency +1",
                "rarity": "Common",
                "price_text": "35 gp",
                "source_table": "Runes",
                "level": 2,
            },
            {
                "Type": "Weapon Property Runes",
                "name": "Flaming",
                "rarity": "Common",
                "price_text": "140 gp",
                "source_table": "Runes",
                "level": 8,
            },
            {
                "Type": "Weapon Property Runes",
                "name": "Frost",
                "rarity": "Uncommon",
                "price_text": "140 gp",
                "source_table": "Runes",
                "level": 8,
            },
            {
                "Type": "Weapon Property Runes",
                "name": "Vorpal",
                "rarity": "Rare",
                "price_text": "5000 gp",
                "source_table": "Runes",
                "level": 10,
            },
        ]
    )


_MOCK_PROPERTY_NAMES = {"Flaming", "Frost", "Vorpal"}

        
def test_striking_rune_present_in_fundamental_candidates():
    runes_df = _load_runes_df()
    all_runes = runes_df.to_dict(orient="records")
    candidates = _fundamental_candidates(all_runes, weapon_level=4, party_level=4)
    names = {str(r.get("name")) for r in candidates}
    assert any(name.lower() == "striking" for name in names)


def test_apply_weapon_runes_can_choose_striking_with_potency():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Striking", case=False)
        | runes_df["name"].str.fullmatch(r"Weapon Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Striking and Potency runes in dataset"

    weapon = {"name": "Test Sword", "level": 4, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.2], seed=1337)

    fused = apply_weapon_runes(
        weapon,
        player_level=4,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {
                "apply_rate": 1.0,
                "potency_weights": {"0": 10, "1": 1},
            },
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    label = fused.get("_rune_fund_label", "")
    assert "+1" in label
    assert "striking" in label.lower()
    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Weapon Potency +1" in rune_names
    assert "Striking" in rune_names


def test_weapon_fundamental_property_gate_can_fail():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Striking", case=False)
        | runes_df["name"].str.fullmatch(r"Weapon Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Striking and Potency runes in dataset"

    weapon = {"name": "Test Sword", "level": 4, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.95], seed=42)

    fused = apply_weapon_runes(
        weapon,
        player_level=4,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {
                "apply_rate": 1.0,
                "potency_weights": {"0": 10, "1": 1},
            },
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Weapon Potency +1" in rune_names
    assert "Striking" not in rune_names
    assert "striking" not in str(fused.get("_rune_fund_label", "")).lower()
    

def test_weapon_fundamental_property_obeys_apply_rate():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Striking", case=False)
        | runes_df["name"].str.fullmatch(r"Weapon Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Striking and Potency runes in dataset"

    weapon = {"name": "Test Sword", "level": 4, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.0], seed=314)

    fused = apply_weapon_runes(
        weapon,
        player_level=4,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {
                "apply_rate": 1.0,
                "potency_weights": {"0": 10, "1": 1},
                "property_pair_rate": 1.0,
            },
            "fundamental property": {"apply_rate": 0.0},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Weapon Potency +1" in rune_names
    assert "Striking" not in rune_names
    assert "striking" not in str(fused.get("_rune_fund_label", "")).lower()


def test_weapon_potency_always_applies_with_candidates():
    runes_df = _load_runes_df()
    mask = runes_df["name"].str.fullmatch(r"Weapon Potency \+1", case=False)
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected a potency rune in dataset"

    weapon = {"name": "Reliable Blade", "level": 4, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1], seed=2024)

    fused = apply_weapon_runes(
        weapon,
        player_level=4,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 0.1},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Weapon Potency +1" in rune_names


def test_armor_potency_always_applies_with_candidates():
    runes_df = _load_runes_df()
    mask = runes_df["name"].str.fullmatch(r"Armor Potency \+1", case=False)
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected an armor potency rune in dataset"

    armor = {"name": "Reliable Armor", "level": 5, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1], seed=2025)

    fused = apply_armor_runes(
        armor,
        player_level=5,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 0.05},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Armor Potency +1" in rune_names


def test_apply_armor_runes_pairs_resilient_when_gate_succeeds():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Resilient", case=False)
        | runes_df["name"].str.fullmatch(r"Armor Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Resilient and Armor Potency runes in dataset"

    armor = {"name": "Reliable Armor", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.2], seed=99)

    fused = apply_armor_runes(
        armor,
        player_level=10,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 1.0},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Armor Potency +1" in rune_names
    assert "Resilient" in rune_names
    label = str(fused.get("_rune_fund_label", ""))
    assert "+1" in label and "resilient" in label.lower()


def test_armor_fundamental_property_gate_can_fail():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Resilient", case=False)
        | runes_df["name"].str.fullmatch(r"Armor Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Resilient and Armor Potency runes in dataset"

    armor = {"name": "Reliable Armor", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.95], seed=199)

    fused = apply_armor_runes(
        armor,
        player_level=10,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 1.0},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Armor Potency +1" in rune_names
    assert "Resilient" not in rune_names
    assert "resilient" not in str(fused.get("_rune_fund_label", "")).lower()


def test_armor_fundamental_property_obeys_apply_rate():
    runes_df = _load_runes_df()
    mask = (
        runes_df["name"].str.fullmatch("Resilient", case=False)
        | runes_df["name"].str.fullmatch(r"Armor Potency \+1", case=False)
    )
    subset = runes_df[mask].copy()
    assert not subset.empty, "Expected Resilient and Armor Potency runes in dataset"

    armor = {"name": "Reliable Armor", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = SequenceRandom([0.0, 0.1, 0.0], seed=815)

    fused = apply_armor_runes(
        armor,
        player_level=10,
        runes_df=subset,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 1.0, "property_pair_rate": 1.0},
            "fundamental property": {"apply_rate": 0.0},
            "property": {"apply_rate": 0.0, "per_slot_rate": 0.0},
        },
    )

    rune_names = {r.get("name") for r in fused.get("runes", [])}
    assert "Armor Potency +1" in rune_names
    assert "Resilient" not in rune_names
    assert "resilient" not in str(fused.get("_rune_fund_label", "")).lower()
    

def test_weapon_property_requires_fundamental_rune():
    runes_df = _mock_weapon_runes_df()
    weapon = {"name": "Simple Blade", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = random.Random(2026)

    fused = apply_weapon_runes(
        weapon,
        player_level=10,
        runes_df=runes_df,
        rng=rng,
        rune_cfg={
            "fundamental": {"apply_rate": 0.0},
            "property": {"apply_rate": 1.0, "per_slot_rate": 1.0},
        },
    )

    assert not fused.get("_rune_fund_label")
    assert not fused.get("_rune_prop_labels")
    prop_names = {
        r.get("name")
        for r in fused.get("runes", [])
        if r.get("name") in _MOCK_PROPERTY_NAMES
    }
    assert not prop_names


def test_weapon_property_per_slot_rate_is_about_thirty_percent():
    runes_df = _mock_weapon_runes_df()
    weapon = {"name": "Reliable Blade", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = random.Random(2027)

    cfg = {
        "fundamental": {"apply_rate": 1.0, "property_pair_rate": 0.0},
        "fundamental": {"apply_rate": 1.0},
        "fundamental property": {"apply_rate": 0.0},
    }

    iterations = 2000
    successes = 0
    for _ in range(iterations):
        fused = apply_weapon_runes(
            weapon,
            player_level=10,
            runes_df=runes_df,
            rng=rng,
            rune_cfg=cfg,
        )
        if fused.get("_rune_prop_labels"):
            successes += 1

    observed = successes / iterations
    assert observed == pytest.approx(0.30, abs=0.05)


def test_weapon_property_rarity_weighting_favors_common():
    runes_df = _mock_weapon_runes_df()
    weapon = {"name": "Reliable Blade", "level": 8, "rarity": "Common", "price_text": "0 gp"}
    rng = random.Random(2028)

    cfg = {
        "fundamental": {"apply_rate": 1.0, "property_pair_rate": 0.0},
        "fundamental": {"apply_rate": 1.0},
        "fundamental property": {"apply_rate": 0.0},
    }

    counts = {"Common": 0, "Uncommon": 0, "Rare": 0}
    iterations = 1000
    for _ in range(iterations):
        fused = apply_weapon_runes(
            weapon,
            player_level=10,
            runes_df=runes_df,
            rng=rng,
            rune_cfg=cfg,
        )
        for rune in fused.get("runes", []):
            if rune.get("name") in _MOCK_PROPERTY_NAMES:
                rarity = str(rune.get("rarity") or "Common").strip().title()
                counts[rarity] = counts.get(rarity, 0) + 1

    assert counts["Common"] > counts["Uncommon"] > counts["Rare"]
    assert counts["Rare"] > 0