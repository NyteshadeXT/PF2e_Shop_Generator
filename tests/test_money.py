import random
import unittest

import pandas as pd

from services.logic import _apply_disposition
from services.money import format_cp, gp_to_cp, multiply_cp, parse_price_to_cp
from services.settings import CONFIG
from services.utils import apply_adjustments_probabilistic, to_gp


class MoneyTests(unittest.TestCase):
    def test_parses_combined_pf2e_denominations(self):
        cases = {
            "2 gp 5 sp 3 cp": 253,
            "1,200 gp, 5 sp": 120_050,
            "5 sp": 50,
            "3 cp": 3,
            "1.5": 150,
            "0 gp": 0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_price_to_cp(text), expected)

    def test_rejects_invalid_or_negative_prices(self):
        for value in ("", "5 gold", "-1 gp", -1, float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(parse_price_to_cp(value))

    def test_formats_all_denominations(self):
        self.assertEqual(format_cp(253), "2 gp 5 sp 3 cp")
        self.assertEqual(format_cp(50), "5 sp")
        self.assertEqual(format_cp(3), "3 cp")
        self.assertEqual(format_cp(0), "0 gp")

    def test_rounds_half_a_copper_up(self):
        self.assertEqual(gp_to_cp("0.005"), 1)
        self.assertEqual(multiply_cp(1, 1.5), 2)
        self.assertEqual(multiply_cp(253, 1.15), 291)

    def test_legacy_gold_parser_accepts_combined_prices(self):
        self.assertEqual(to_gp("2 gp 5 sp 3 cp"), 2.53)

    def test_disposition_names_match_price_behavior(self):
        self.assertEqual(_apply_disposition(10, "greedy"), 11.5)
        self.assertEqual(_apply_disposition(10, "fair"), 10.0)
        self.assertEqual(_apply_disposition(10, "generous"), 9.0)
        mults = CONFIG["disposition_multipliers"]
        self.assertGreater(mults["greedy"], mults["fair"])
        self.assertGreater(mults["fair"], mults["generous"])

    def test_adjustment_adds_combined_prices_exactly(self):
        items = [{
            "name": "Test Armor",
            "category": "Armor",
            "level": 1,
            "rarity": "Common",
            "price": "2 gp 5 sp 3 cp",
        }]
        adjustments = pd.DataFrame([{
            "name": "Fine",
            "subtype": "Armor",
            "rarity": "Common",
            "level": 1,
            "price_text": "7 sp 8 cp",
        }])
        result = apply_adjustments_probabilistic(
            items,
            adjustments,
            {"armor": 1.0},
            rng=random.Random(7),
        )
        self.assertEqual(result[0]["price"], "3 gp 3 sp 1 cp")


if __name__ == "__main__":
    unittest.main()
