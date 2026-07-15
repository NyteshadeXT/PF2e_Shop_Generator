import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.catalog_validation import CatalogValidationError, validate_catalog


REQUIRED_SOURCES = (
    "mundane",
    "weapon_basic",
    "armor_basic",
    "shield_basic",
    "runes",
    "scrolls",
    "staff_wand",
    "specific_magic_weapons",
    "specific_magic_armor",
    "specific_magic_shields",
)


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "catalog.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE catalog (
                    category TEXT, source_table TEXT, source_id TEXT, name TEXT,
                    level INTEGER, rarity TEXT, type TEXT, subtype TEXT,
                    price_text TEXT, tags TEXT, Bulk TEXT, Source TEXT,
                    Publisher_Source TEXT, shop_type TEXT, stock_flag INTEGER
                );
                CREATE VIEW v_items_norm AS SELECT * FROM catalog;
                CREATE TABLE Spells (
                    Name TEXT, Rank INTEGER, Tradition TEXT, Rarity TEXT, Cost REAL
                );
                CREATE TABLE Formula (ItemLevel INTEGER, Cost TEXT);
                CREATE TABLE Adjustments (
                    Name TEXT, ItemLevel INTEGER, Subtype TEXT, Cost TEXT, Rarity TEXT
                );
                CREATE TABLE Armor_Material (
                    Name TEXT, ItemLevel INTEGER, AddedPrice REAL, Prerequisite TEXT
                );
                CREATE TABLE Weapon_Material (
                    Name TEXT, ItemLevel INTEGER, AddedPrice REAL, Prerequisite TEXT
                );
                CREATE TABLE Shield_Material (
                    Name TEXT, ItemLevel INTEGER, AddedPrice REAL, Prerequisite TEXT
                );
                INSERT INTO Spells VALUES ('Light', 1, 'Arcane', 'Common', 1);
                INSERT INTO Formula VALUES (1, '1 gp');
                INSERT INTO Adjustments VALUES ('Fine', 1, 'Weapon', '1 gp', 'Common');
                INSERT INTO Armor_Material VALUES ('Steel', 1, 1, 'heavy');
                INSERT INTO Weapon_Material VALUES ('Steel', 1, 1, 'sword');
                INSERT INTO Shield_Material VALUES ('Steel', 1, 1, 'shield');
                """
            )
            connection.executemany(
                "INSERT INTO catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "Item",
                        source,
                        str(index),
                        f"Item {index}",
                        index,
                        "Common",
                        "Item",
                        "",
                        "1 gp",
                        "",
                        "1",
                        "Test Source",
                        "Paizo",
                        "General",
                        1,
                    )
                    for index, source in enumerate(REQUIRED_SOURCES, start=1)
                ],
            )
            connection.commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_valid_catalog_returns_semantic_counts(self):
        report = validate_catalog(self.database, "v_items_norm", minimum_rows=1)

        self.assertEqual(report["rows"], len(REQUIRED_SOURCES))
        self.assertEqual(report["sources"], len(REQUIRED_SOURCES))
        self.assertEqual(report["blank_prices"], 0)

    def test_invalid_item_content_is_rejected(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE catalog SET name = '' WHERE source_id = '1'")
            connection.commit()

        with self.assertRaisesRegex(CatalogValidationError, "blank item names"):
            validate_catalog(self.database, "v_items_norm", minimum_rows=1)

    def test_duplicate_source_identifier_is_rejected(self):
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT * FROM catalog WHERE source_id = '1'").fetchone()
            connection.execute(
                "INSERT INTO catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            connection.commit()

        with self.assertRaisesRegex(CatalogValidationError, "duplicated source identifiers"):
            validate_catalog(self.database, "v_items_norm", minimum_rows=1)

    def test_unparseable_price_is_rejected(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE catalog SET price_text = 'many coins' WHERE source_id = '1'"
            )
            connection.commit()

        with self.assertRaisesRegex(CatalogValidationError, "invalid price values"):
            validate_catalog(self.database, "v_items_norm", minimum_rows=1)


if __name__ == "__main__":
    unittest.main()
