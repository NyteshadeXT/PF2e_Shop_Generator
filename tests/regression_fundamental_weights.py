from collections import Counter
import random

from services.logic import _weighted_pick_by_rarity, _weighted_pick_fundamental
from services.utils import parse_potency_rank


def _sample_potency_counts(cfg):
    candidates = [
        {"name": "Weapon Potency +1"},
        {"name": "Weapon Potency +2"},
        {"name": "Weapon Potency +3"},
    ]
    rng = random.Random(8675309)
    counts = Counter()
    for _ in range(6000):
        picked = _weighted_pick_fundamental(candidates, rng, cfg)
        counts[parse_potency_rank(picked.get("name"))] += 1
    return counts


def test_configured_weights_prefer_higher_potency():
    counts = _sample_potency_counts(
        {"fundamental": {"potency_weights": {"1": 1, "2": 2, "3": 3}}}
    )
    assert counts[3] > counts[2] > counts[1]


def test_derived_weights_prefer_higher_potency_when_missing_config():
    counts = _sample_potency_counts({"fundamental": {}})
    assert counts[3] > counts[2] > counts[1]


def test_builder_level_priority_favors_potency_closest_to_maximum_level():
    candidates = [
        {"name": "Weapon Potency +1", "level": 2},
        {"name": "Weapon Potency +2", "level": 10},
    ]
    cfg = {
        "fundamental": {"potency_weights": {"1": 1, "2": 2}},
        "_prefer_higher_level": True,
        "_target_level": 10,
    }
    rng = random.Random(112358)
    counts = Counter()
    for _ in range(3000):
        picked = _weighted_pick_fundamental(candidates, rng, cfg)
        counts[picked["name"]] += 1

    assert counts["Weapon Potency +2"] > counts["Weapon Potency +1"] * 10


def test_builder_level_priority_favors_property_closest_to_maximum_level():
    candidates = [
        {"name": "Lower Rune", "level": 7, "rarity": "Common"},
        {"name": "Near Rune", "level": 10, "rarity": "Common"},
    ]
    rng = random.Random(271828)
    counts = Counter()
    for _ in range(3000):
        picked = _weighted_pick_by_rarity(
            candidates,
            rng,
            {"Common": 1.0},
            target_level=10,
        )
        counts[picked["name"]] += 1

    assert counts["Near Rune"] > counts["Lower Rune"] * 10
