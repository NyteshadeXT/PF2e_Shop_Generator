import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from services import db


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "references.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE Spells (
                    Name TEXT, Rank INTEGER, Tradition TEXT, Rarity TEXT,
                    Cost REAL, Traits TEXT, Source TEXT
                );
                INSERT INTO Spells VALUES
                    ('Ignition', 1, 'Arcane,Primal', 'Common', 1, 'FIRE', 'Player Core'),
                    ('Heal', 1, 'Divine,Primal', 'Common', 1, 'HEALING', 'Player Core');
                CREATE TABLE Formula (Level INTEGER, Price TEXT);
                INSERT INTO Formula VALUES (1, '1 gp'), (2, '2 gp');
                """
            )
        db.clear_reference_caches()

    def tearDown(self):
        db.clear_reference_caches()
        self.tempdir.cleanup()

    def _count_connections(self):
        original = db.sqlite3.connect
        calls = []
        lock = threading.Lock()

        def connect(*args, **kwargs):
            with lock:
                calls.append(args[0] if args else kwargs.get("database"))
            return original(*args, **kwargs)

        return calls, connect

    def test_concurrent_spell_loads_share_one_database_query(self):
        calls, connect = self._count_connections()
        with patch("services.db.sqlite3.connect", side_effect=connect):
            with ThreadPoolExecutor(max_workers=8) as pool:
                frames = list(
                    pool.map(
                        lambda _: db.load_spells(sqlite_path=str(self.database)),
                        range(16),
                    )
                )
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(len(frame) == 2 for frame in frames))

    def test_spell_cache_refreshes_when_database_changes(self):
        first = db.load_spells(sqlite_path=str(self.database))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO Spells VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Fear", 1, "Arcane,Divine,Occult,Primal", "Common", 1, "FEAR", "Player Core"),
            )
            connection.commit()
        os.utime(self.database, None)
        second = db.load_spells(sqlite_path=str(self.database))
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 3)

    def test_formula_rows_are_cached(self):
        original_path = db.CONFIG["sqlite_db_path"]
        calls, connect = self._count_connections()
        db.CONFIG["sqlite_db_path"] = str(self.database)
        try:
            with patch("services.db.sqlite3.connect", side_effect=connect):
                first = db.load_formula_rows()
                second = db.load_formula_rows()
        finally:
            db.CONFIG["sqlite_db_path"] = original_path
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.to_dict(orient="records"), second.to_dict(orient="records"))


if __name__ == "__main__":
    unittest.main()
