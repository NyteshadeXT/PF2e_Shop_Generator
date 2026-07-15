import unittest

import pandas as pd

from services.logic import _filter_source_tables


class SourceFilterTests(unittest.TestCase):
    def setUp(self):
        self.catalog = pd.DataFrame(
            [
                {"name": "Sword", "source_table": "weapon_basic", "category": "Weapon"},
                {"name": "Armor", "source_table": "armor_basic", "category": "Armor"},
                {"name": "Wand", "source_table": "staff_wand", "category": "Magic"},
            ]
        )

    def test_matching_source_is_selected(self):
        result = _filter_source_tables(self.catalog, ["weapon_basic"])
        self.assertEqual(result["name"].tolist(), ["Sword"])

    def test_typo_fails_closed_instead_of_leaking_category(self):
        result = _filter_source_tables(self.catalog, ["weapon_bsaic"])
        self.assertTrue(result.empty)

    def test_missing_source_column_fails_closed(self):
        result = _filter_source_tables(self.catalog.drop(columns=["source_table"]), ["weapon_basic"])
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
