import unittest
from unittest.mock import patch

import pandas as pd

from services import generation


def _item(name, *, rarity="Common", critical=False, **extra):
    return {
        "name": name,
        "price": "1 gp",
        "rarity": rarity,
        "level": 1,
        "critical": critical,
        **extra,
    }


class GenerationServiceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = pd.DataFrame([{"shop_type": "General"}])
        self.submitted = {
            "shop_type": "general",
            "shop_size": "small",
            "disposition": "fair",
            "party_level": "5",
            "shop_name": "The Deterministic Dragon",
            "seed": "service-test-seed",
        }

    def test_validates_and_canonicalizes_generation_settings(self):
        result = generation.validate_generation_inputs(self.submitted, self.catalog)
        self.assertEqual(
            result,
            ("General", "small", "fair", "The Deterministic Dragon", 5),
        )

    def test_rejects_invalid_settings_without_flask_dependencies(self):
        for field, value in (
            ("shop_type", "Unknown"),
            ("shop_size", "enormous"),
            ("disposition", "hostile"),
            ("party_level", "five"),
            ("party_level", "99"),
            ("shop_name", "x" * 101),
        ):
            submitted = dict(self.submitted, **{field: value})
            with self.subTest(field=field, value=value):
                with self.assertRaises(generation.GenerationInputError):
                    generation.validate_generation_inputs(submitted, self.catalog)

    def test_orchestration_builds_one_complete_deterministic_snapshot(self):
        mundane = [_item("Rope", critical=True)]
        materials = [_item("Cold Iron", rarity="Uncommon")]
        armor_basic = [
            _item("Chain Shirt", critical=True),
            _item(
                "Runed Mail",
                category="Runed Armor",
                is_magic_countable=True,
            ),
        ]
        magic_armor = [_item("Dragonplate", rarity="Rare", critical=True)]
        weapons_basic = [
            _item("Club"),
            _item(
                "Runed Blade",
                critical=True,
                category="Runed Weapon",
                is_magic_countable=True,
            ),
        ]
        magic_weapons = [
            _item(
                "Flaming Sword",
                rarity="Uncommon",
                critical=True,
                is_magic_countable=True,
            )
        ]
        magic = [_item("Wand", rarity="Rare", critical=True)]
        spellbooks = [_item("Arcane Spellbook")]
        formulas = [_item("Formula: Lantern")]

        selectors = {
            "select_mundane_items": {"items": mundane},
            "select_materials": {"items": materials},
            "select_armor_items": {"items": armor_basic},
            "select_specific_magic_armor": {"items": magic_armor},
            "select_weapons_items": {"items": weapons_basic},
            "select_specific_magic_weapons": {"items": magic_weapons},
            "select_magic_items": {"items": magic, "window": (3, 6)},
            "select_spellbooks": {"items": spellbooks},
            "select_formulas": {"items": formulas},
        }
        patches = [patch.object(generation, name, return_value=value) for name, value in selectors.items()]
        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

        with (
            patch.object(generation, "generation_fingerprint", return_value="build-123"),
            patch.object(generation, "create_reproduction_key", return_value="portable-key"),
        ):
            first = generation.generate_shop_snapshot(self.catalog, self.submitted)
            second = generation.generate_shop_snapshot(self.catalog, self.submitted)

        self.assertEqual(first, second)
        self.assertEqual(
            first["shop"],
            {
                "shop_name": "The Deterministic Dragon",
                "shop_type": "General",
                "shop_size": "small",
                "disposition": "fair",
                "party_level": 5,
                "seed": "service-test-seed",
                "reproduction_key": "portable-key",
                "generation_fingerprint": "build-123",
                "window": (3, 6),
            },
        )
        self.assertEqual(first["lists"]["armor_items"][:2], armor_basic)
        self.assertEqual(first["lists"]["armor_items"][2]["name"], "Dragonplate")
        self.assertTrue(first["lists"]["armor_items"][2]["is_magic_countable"])
        self.assertEqual(first["lists"]["weapon_items"][:2], weapons_basic)
        self.assertEqual(first["lists"]["weapon_items"][2]["name"], "Flaming Sword")
        self.assertTrue(first["lists"]["weapon_items"][2]["is_magic_countable"])
        self.assertEqual(first["lists"]["magic_items"], magic + spellbooks)
        self.assertEqual(first["lists"]["formula_items"], formulas)

        summary = first["summary"]
        self.assertEqual(
            summary["counts"],
            {"common": 6, "uncommon": 2, "rare": 2, "unique": 0},
        )
        self.assertEqual(summary["picked"]["weapons"], 1)
        self.assertEqual(summary["picked"]["armor"], 1)
        self.assertEqual(summary["picked"]["magic"], 6)
        self.assertEqual(summary["picked"]["critical"], 6)
        self.assertEqual(summary["picked"]["critical_armor_shield"], 1)
        self.assertEqual(summary["picked"]["critical_magic"], 4)
        self.assertEqual(
            summary["picked"]["critical"],
            summary["picked"]["critical_mundane"]
            + summary["picked"]["critical_materials"]
            + summary["picked"]["critical_armor_shield"]
            + summary["picked"]["critical_weapons"]
            + summary["picked"]["critical_magic"],
        )

    def test_mismatched_reproduction_key_warns_and_uses_current_fingerprint(self):
        restored = {
            "seed": "restored-seed",
            "shop_type": "General",
            "shop_size": "small",
            "disposition": "fair",
            "party_level": 5,
            "_generation_fingerprint": "older-build",
        }
        empty = {"items": []}
        with (
            patch.object(generation, "parse_reproduction_key", return_value=restored),
            patch.object(generation, "generation_fingerprint", return_value="current-build"),
            patch.object(generation, "create_reproduction_key", return_value="new-key"),
            patch.object(generation, "select_mundane_items", return_value=empty),
            patch.object(generation, "select_materials", return_value=empty),
            patch.object(generation, "select_armor_items", return_value=empty),
            patch.object(generation, "select_specific_magic_armor", return_value=empty),
            patch.object(generation, "select_weapons_items", return_value=empty),
            patch.object(generation, "select_specific_magic_weapons", return_value=empty),
            patch.object(generation, "select_magic_items", return_value=empty),
            patch.object(generation, "select_spellbooks", return_value=empty),
            patch.object(generation, "select_formulas", return_value=empty),
        ):
            snapshot = generation.generate_shop_snapshot(self.catalog, self.submitted)

        self.assertEqual(snapshot["shop"]["seed"], "restored-seed")
        self.assertEqual(snapshot["shop"]["generation_fingerprint"], "current-build")
        self.assertIn("different catalog or generator build", snapshot["summary"]["reproduction_warning"])


if __name__ == "__main__":
    unittest.main()
