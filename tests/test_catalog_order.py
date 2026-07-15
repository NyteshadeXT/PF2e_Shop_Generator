import unittest

import pandas as pd

from services.catalog_order import canonicalize_frame, canonicalize_records
from services.logic import select_items_by_source
from services.randomness import generation_rng


class CatalogOrderTests(unittest.TestCase):
    def setUp(self):
        self.catalog = pd.DataFrame(
            [
                {
                    "category": "Mundane",
                    "source_table": "mundane",
                    "source_id": str(index),
                    "name": f"General Item {index:02d}",
                    "level": index % 6,
                    "rarity": "Common" if index % 3 else "Uncommon",
                    "price_text": f"{index} gp",
                    "tags": "",
                    "shop_type": "General",
                    "stock_flag": 1,
                    "Bulk": "1",
                    "Source": "Test Source",
                    "subtype": "gear",
                    "Publisher_Source": "Paizo",
                }
                for index in range(1, 21)
            ]
        )

    def test_frame_order_depends_on_content_not_input_index(self):
        shuffled = self.catalog.sample(frac=1, random_state=91)

        first = canonicalize_frame(self.catalog)
        second = canonicalize_frame(shuffled)

        self.assertEqual(
            first.to_dict(orient="records"),
            second.to_dict(orient="records"),
        )
        self.assertEqual(first.index.tolist(), list(range(len(first))))

    def test_record_order_depends_on_complete_content(self):
        records = [{"name": "Beta", "level": 1}, {"name": "Alpha", "level": 2}]

        self.assertEqual(
            canonicalize_records(reversed(records)),
            canonicalize_records(records),
        )

    def test_same_seed_ignores_catalog_row_order(self):
        shuffled = self.catalog.sample(frac=1, random_state=37).reset_index(drop=True)

        with generation_rng("catalog-order-seed"):
            first = select_items_by_source(
                self.catalog,
                ["mundane"],
                "mundane",
                "General",
                5,
                "small",
                "fair",
            )
        with generation_rng("catalog-order-seed"):
            second = select_items_by_source(
                shuffled,
                ["mundane"],
                "mundane",
                "General",
                5,
                "small",
                "fair",
            )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
