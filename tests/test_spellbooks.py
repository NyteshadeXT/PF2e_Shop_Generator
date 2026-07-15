import unittest
from unittest.mock import patch

from services.randomness import generation_rng
from services.settings import CONFIG
from services import db, spellbooks


class _FixedD4:
    def randint(self, low, high):
        return 1


class SpellbookTests(unittest.TestCase):
    def setUp(self):
        db.clear_reference_caches()

    def test_algorithm_matches_legacy_count_pattern_for_all_levels(self):
        with patch("services.spellbooks.get_rng", return_value=_FixedD4()):
            for level in range(1, 19):
                counts = spellbooks._counts_for_book_level(level)
                highest = (level + 1) // 2
                for rank in range(1, 11):
                    if rank > highest:
                        expected = 0
                    elif level % 2 and rank == highest:
                        expected = 3
                    else:
                        expected = 4
                    self.assertEqual(counts[rank], expected, (level, rank))

            self.assertEqual(
                spellbooks._counts_for_book_level(19),
                {1: 4, 2: 4, 3: 4, 4: 4, 5: 4, 6: 4, 7: 4, 8: 4, 9: 4, 10: 2},
            )
            self.assertEqual(
                spellbooks._counts_for_book_level(20),
                {1: 5, 2: 4, 3: 4, 4: 4, 5: 4, 6: 3, 7: 3, 8: 2, 9: 2, 10: 2},
            )

    def _tracked_connect(self):
        statements = []
        original = db.sqlite3.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        return statements, connect

    def test_standalone_builder_uses_one_spell_query(self):
        statements, connect = self._tracked_connect()
        with patch("services.db.sqlite3.connect", side_effect=connect):
            with generation_rng("one-query-builder"):
                result = spellbooks.build_spellbook(
                    tradition="Arcane",
                    book_level=12,
                    sqlite_path=CONFIG["sqlite_db_path"],
                )
                spellbooks.build_spellbook(
                    tradition="Arcane",
                    book_level=12,
                    sqlite_path=CONFIG["sqlite_db_path"],
                )
        spell_queries = [sql for sql in statements if "FROM Spells" in sql]
        self.assertEqual(len(spell_queries), 1)
        self.assertTrue(result)
        self.assertTrue(all("source" in row for row in result))

    def test_multi_book_generation_reuses_tradition_pool(self):
        statements, connect = self._tracked_connect()
        original_cfg = dict(CONFIG.get("spellbooks", {}))
        CONFIG["spellbooks"] = {"drop_rate": 1.0, "max_books": 3}
        try:
            with patch("services.db.sqlite3.connect", side_effect=connect):
                with generation_rng("one-query-shop-books"):
                    result = spellbooks.select_spellbooks(
                        df=None,
                        shop_type="Arcane",
                        party_level=12,
                        shop_size="large",
                        disposition="fair",
                        sqlite_path=CONFIG["sqlite_db_path"],
                    )
        finally:
            CONFIG["spellbooks"] = original_cfg
        spell_queries = [sql for sql in statements if "FROM Spells" in sql]
        self.assertEqual(len(spell_queries), 1)
        self.assertEqual(result["base_count"], 3)

    def test_theme_filter_is_applied_in_memory(self):
        with generation_rng("fire-theme"):
            result = spellbooks.build_spellbook(
                tradition="Arcane",
                book_level=10,
                themes=["fire"],
                sqlite_path=CONFIG["sqlite_db_path"],
            )
        self.assertTrue(result)
        self.assertTrue(all("FIRE" in row["traits"].upper() for row in result))


if __name__ == "__main__":
    unittest.main()
