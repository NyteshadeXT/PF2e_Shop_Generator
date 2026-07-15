import tempfile
import unittest
import os
from pathlib import Path

from services.player_views import (
    SnapshotNotFound,
    current_token,
    load_snapshot,
    save_snapshot,
)


class PlayerViewStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "views.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_shared_token_is_immutable_and_repeatable(self):
        token = "a" * 32
        snapshot = {"shop": {"shop_name": "Test"}, "lists": {"magic_items": [{"name": "Wand"}]}}
        save_snapshot(token, "game-one", snapshot, db_path=self.db)

        self.assertEqual(load_snapshot(token, "game-one", db_path=self.db), snapshot)
        self.assertEqual(load_snapshot(token, "game-one", db_path=self.db), snapshot)

    def test_channels_are_isolated(self):
        save_snapshot("a" * 32, "game-one", {"lists": {"magic_items": [1]}}, db_path=self.db)
        save_snapshot("b" * 32, "game-two", {"lists": {"magic_items": [2]}}, db_path=self.db)

        self.assertEqual(current_token("game-one", db_path=self.db), "a" * 32)
        self.assertEqual(current_token("game-two", db_path=self.db), "b" * 32)
        with self.assertRaises(SnapshotNotFound):
            load_snapshot("a" * 32, "game-two", db_path=self.db)

    def test_new_snapshot_advances_only_its_live_channel(self):
        save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)

        self.assertEqual(current_token("game-one", db_path=self.db), "b" * 32)
        self.assertEqual(load_snapshot("a" * 32, "game-one", db_path=self.db), {"version": 1})

    def test_missing_shared_view_does_not_regenerate(self):
        previous = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.db)
        try:
            import app

            original = app._build_payload
            app._build_payload = lambda *args, **kwargs: self.fail("generation must not run")
            try:
                response = app.app.test_client().get(
                    "/player-view?channel=game-one&roll_id=" + "f" * 32
                )
            finally:
                app._build_payload = original
            self.assertEqual(response.status_code, 404)
        finally:
            if previous is None:
                os.environ.pop("LOOTGEN_STATE_DB_PATH", None)
            else:
                os.environ["LOOTGEN_STATE_DB_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
