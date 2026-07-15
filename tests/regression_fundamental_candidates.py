from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.logic import _fundamental_candidates, _fundamental_candidates_armor


WEAPON_RUNES = [
    {"Type": "Weapon Fundamental Rune", "name": "Weapon Potency +1", "level": 2},
    {"Type": "Weapon Fundamental Rune", "name": "Weapon Potency +2", "level": 10},
    {"Type": "Weapon Fundamental Property", "name": "Weapon Fundamental Property", "level": 3},
]


ARMOR_RUNES = [
    {"Type": "Armor Fundamental Rune", "name": "Armor Potency +1", "level": 5},
    {"Type": "Armor Fundamental Rune", "name": "Armor Potency +2", "level": 11},
]


def names(rows):
    return {row["name"] for row in rows}


def test_weapon_fundamentals_hold_until_party_level_two():
    assert _fundamental_candidates(WEAPON_RUNES, weapon_level=0, party_level=1) == []


def test_weapon_fundamentals_available_at_party_level_two_even_for_level_one_weapon():
    picks = _fundamental_candidates(WEAPON_RUNES, weapon_level=0, party_level=2)
    assert names(picks) == {"Weapon Potency +1", "Weapon Fundamental Property"}


def test_weapon_fundamental_cap_respects_party_level():
    picks = _fundamental_candidates(WEAPON_RUNES, weapon_level=0, party_level=10)
    assert "Weapon Potency +2" in names(picks)


def test_armor_fundamentals_hold_until_party_level_five():
    assert _fundamental_candidates_armor(ARMOR_RUNES, armor_level=0, party_level=4) == []


def test_armor_fundamentals_available_at_party_level_five_even_for_level_one_quarter_armor():
    picks = _fundamental_candidates_armor(ARMOR_RUNES, armor_level=0, party_level=5)
    assert names(picks) == {"Armor Potency +1"}


def test_armor_fundamental_cap_respects_party_level():
    picks = _fundamental_candidates_armor(ARMOR_RUNES, armor_level=0, party_level=12)
    assert names(picks) == {"Armor Potency +1", "Armor Potency +2"}