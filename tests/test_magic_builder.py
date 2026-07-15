import unittest
from unittest.mock import patch

import pandas as pd
from flask import Flask

from services import magic_builder


class MagicBuilderTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(magic_builder.bp)
        self.client = app.test_client()
        self.catalog = pd.DataFrame(
            [
                {
                    "name": "Longsword",
                    "category": "Weapon",
                    "type": "Martial",
                    "source_table": "weapon_basic",
                    "level": 0,
                    "rarity": "Common",
                    "price_text": "1 gp",
                    "Bulk": "1",
                    "Source": "Player Core",
                    "tags": "versatile",
                },
                {
                    "name": "Leather Armor",
                    "category": "Armor",
                    "type": "Light Armor",
                    "source_table": "armor_basic",
                    "level": 0,
                    "rarity": "Common",
                    "price_text": "2 gp",
                    "Bulk": "1",
                    "Source": "Player Core",
                    "tags": "comfort",
                },
                {
                    "name": "Steel Shield",
                    "category": "Shield",
                    "type": "Shield",
                    "source_table": "shield_basic",
                    "level": 0,
                    "rarity": "Common",
                    "price_text": "2 gp",
                    "Bulk": "1",
                    "Source": "Player Core",
                    "tags": "shield",
                },
            ]
        )

    def test_lists_valid_weapon_armor_and_shield_bases(self):
        cases = (
            ({"type": "weapon", "subtype": "martial", "max_level": 5}, "Longsword"),
            ({"type": "armor", "armor_type": "light", "max_level": 5}, "Leather Armor"),
            ({"type": "shield", "max_level": 5}, "Steel Shield"),
        )
        with patch.object(magic_builder, "load_items", return_value=self.catalog):
            for query, expected in cases:
                with self.subTest(item_type=query["type"]):
                    response = self.client.get(
                        "/api/magic-builder/bases", query_string=query
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json, {"ok": True, "names": [expected]})

    def test_builds_weapon_armor_and_shield_successfully(self):
        cases = (
            ("weapon", "Longsword", "apply_weapon_runes"),
            ("armor", "Leather Armor", "apply_armor_runes"),
            ("shield", "Steel Shield", "apply_shield_runes"),
        )

        def apply_runes(item, **_kwargs):
            return dict(item)

        with (
            patch.object(magic_builder, "load_items", return_value=self.catalog),
            patch.object(magic_builder, "_load_runes_df", return_value=pd.DataFrame()),
            patch.object(magic_builder, "_compose_weapon_name", return_value=""),
            patch.object(magic_builder, "_compose_armor_name", return_value=""),
        ):
            for item_type, base_name, applier_name in cases:
                with self.subTest(item_type=item_type):
                    with patch.object(magic_builder, applier_name, side_effect=apply_runes):
                        response = self.client.post(
                            "/api/magic-builder/build",
                            json={
                                "item_type": item_type,
                                "base_name": base_name,
                                "max_level": 5,
                            },
                        )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["ok"])
                    self.assertEqual(response.json["item"]["_base_name"], base_name)
                    self.assertEqual(response.json["item"]["aon_target"], base_name)
                    self.assertNotEqual(response.json["item"]["name"], base_name)

    def test_reroll_is_deterministic_and_changes_the_builder_rng(self):
        def apply_runes(item, *, rng, **_kwargs):
            result = dict(item)
            result["test_roll"] = rng.random()
            return result

        def build(reroll):
            response = self.client.post(
                "/api/magic-builder/build",
                json={
                    "item_type": "weapon",
                    "base_name": "Longsword",
                    "max_level": 10,
                    "reroll": reroll,
                },
            )
            self.assertEqual(response.status_code, 200)
            return response.json["item"]["test_roll"]

        with (
            patch.object(magic_builder, "load_items", return_value=self.catalog),
            patch.object(magic_builder, "_load_runes_df", return_value=pd.DataFrame()),
            patch.object(magic_builder, "apply_weapon_runes", side_effect=apply_runes),
            patch.object(magic_builder, "_compose_weapon_name", return_value=""),
        ):
            first = build(12)
            repeated = build(12)
            changed = build(13)

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)

    def test_builder_forces_only_fundamental_rune_rate(self):
        configured = {
            "fundamental": {"apply_rate": 0.25},
            "properties": {"apply_rate": 0.4},
            "fundamental_apply_rate": 0.5,
            "property_apply_rate": 0.6,
        }
        with patch.dict(
            magic_builder.CONFIG, {"weapon_runes": configured}, clear=False
        ):
            result = magic_builder._runes_config("weapon")

        self.assertEqual(result["fundamental"]["apply_rate"], 1.0)
        self.assertEqual(result["fundamental_apply_rate"], 1.0)
        self.assertEqual(result["properties"]["apply_rate"], 0.4)
        self.assertEqual(result["property_apply_rate"], 0.6)
        self.assertEqual(configured["fundamental"]["apply_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()
