import json
import unittest
from unittest.mock import patch

from flask import Flask

from services import curation


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.app.register_blueprint(curation.bp)

        @self.app.get("/results/<roll_id>", endpoint="results_view")
        def results_view(roll_id):
            return roll_id

        self.client = self.app.test_client()
        self.snapshot = {
            "shop": {
                "shop_type": "General",
                "shop_size": "small",
                "disposition": "fair",
                "party_level": 5,
                "seed": "original",
            },
            "lists": {
                "mundane_items": [{"name": "Rope", "quantity": 1, "rarity": "Common", "level": 0, "price": "1 sp"}],
                "material_items": [],
                "formula_items": [],
                "armor_items": [],
                "weapon_items": [],
                "magic_items": [],
            },
            "summary": {},
        }

    def test_quantity_edit_saves_a_new_unpublished_revision(self):
        saved = {}

        def capture(token, channel, snapshot, **kwargs):
            saved.update(token=token, channel=channel, snapshot=snapshot, kwargs=kwargs)

        with (
            patch.object(curation, "load_snapshot", return_value=self.snapshot),
            patch.object(curation, "save_snapshot", side_effect=capture),
        ):
            response = self.client.post(
                "/results/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/curate",
                data={
                    "channel": "campaign",
                    "operation": "quantity",
                    "section": "mundane",
                    "item_index": "0",
                    "quantity": "7",
                },
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(saved["snapshot"]["lists"]["mundane_items"][0]["quantity"], 7)
        self.assertTrue(saved["snapshot"]["curation"]["is_curated"])
        self.assertEqual(saved["snapshot"]["curation"]["revision"], 1)
        self.assertFalse(saved["kwargs"]["advance_channel"])

    def test_builder_weapon_is_added_to_weapon_section_and_magic_summary(self):
        saved = {}
        builder_item = {
            "item_type": "weapon",
            "name": "+1 Striking Greatpick",
            "level": 4,
            "rarity": "Common",
            "price": "101 gp",
            "category": "Runed Weapon",
        }

        with (
            patch.object(curation, "load_snapshot", return_value=self.snapshot),
            patch.object(curation, "save_snapshot", side_effect=lambda token, channel, snapshot, **kwargs: saved.update(snapshot=snapshot)),
        ):
            response = self.client.post(
                "/results/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/curate",
                data={
                    "channel": "campaign",
                    "operation": "add_builder",
                    "item_json": json.dumps(builder_item),
                },
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(saved["snapshot"]["lists"]["weapon_items"][0]["name"], "+1 Striking Greatpick")
        self.assertEqual(saved["snapshot"]["summary"]["picked"]["weapons"], 0)
        self.assertEqual(saved["snapshot"]["summary"]["picked"]["magic"], 1)

    def test_item_can_be_hidden_and_revealed_in_new_revisions(self):
        saved = {}
        with (
            patch.object(curation, "load_snapshot", return_value=self.snapshot),
            patch.object(curation, "save_snapshot", side_effect=lambda token, channel, snapshot, **kwargs: saved.update(snapshot=snapshot)),
        ):
            hidden = self.client.post(
                "/results/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/curate",
                data={"channel": "campaign", "operation": "hide", "section": "mundane", "item_index": "0"},
            )
        self.assertEqual(hidden.status_code, 303)
        self.assertTrue(saved["snapshot"]["lists"]["mundane_items"][0]["player_hidden"])

        with (
            patch.object(curation, "load_snapshot", return_value=saved["snapshot"]),
            patch.object(curation, "save_snapshot", side_effect=lambda token, channel, snapshot, **kwargs: saved.update(snapshot=snapshot)),
        ):
            revealed = self.client.post(
                "/results/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/curate",
                data={"channel": "campaign", "operation": "reveal", "section": "mundane", "item_index": "0"},
            )
        self.assertEqual(revealed.status_code, 303)
        self.assertFalse(saved["snapshot"]["lists"]["mundane_items"][0]["player_hidden"])


if __name__ == "__main__":
    unittest.main()
