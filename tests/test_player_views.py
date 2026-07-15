import tempfile
import unittest
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import services.player_views as player_view_storage

from services.player_views import (
    DuplicateGeneration,
    LiveChannelNotFound,
    SnapshotConflict,
    SnapshotNotFound,
    backup_database,
    channel_summaries,
    cleanup_snapshots,
    current_token,
    initialize,
    generation_request_snapshot,
    live_channel,
    load_snapshot,
    recent_snapshots,
    rotate_live_token,
    save_snapshot,
    set_current_snapshot,
    snapshot_stats,
    snapshot_count,
)
from services.settings import CONFIG


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

    def test_initialization_is_cached_after_first_success(self):
        initialize(self.db)

        with patch.object(
            player_view_storage,
            "_connect",
            wraps=player_view_storage._connect,
        ) as connect:
            initialize(self.db)

        connect.assert_not_called()

    def test_state_database_uses_wal_for_concurrent_readers(self):
        initialize(self.db)

        with closing(sqlite3.connect(self.db)) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")

    def test_online_backup_is_complete_and_readable(self):
        snapshot = {"shop": {"shop_name": "Backed Up"}, "lists": {"magic_items": []}}
        save_snapshot("a" * 32, "game-one", snapshot, db_path=self.db)
        destination = Path(self.tempdir.name) / "backups" / "player-views.db"

        created = backup_database(destination, db_path=self.db)

        self.assertEqual(created, destination.resolve())
        self.assertEqual(load_snapshot("a" * 32, "game-one", db_path=created), snapshot)
        self.assertEqual(snapshot_stats(db_path=created)["snapshots"], 1)
        with closing(sqlite3.connect(created)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_refuses_to_replace_active_database(self):
        with self.assertRaises(ValueError):
            backup_database(self.db, db_path=self.db)

    def test_shared_token_cannot_be_overwritten(self):
        token = "a" * 32
        original = {"shop": {"shop_name": "Original"}, "lists": {"magic_items": []}}
        save_snapshot(token, "game-one", original, db_path=self.db)

        with self.assertRaises(SnapshotConflict):
            save_snapshot(
                token,
                "game-one",
                {"shop": {"shop_name": "Changed"}, "lists": {"magic_items": []}},
                db_path=self.db,
            )

        self.assertEqual(load_snapshot(token, "game-one", db_path=self.db), original)

    def test_saving_identical_snapshot_is_idempotent(self):
        token = "a" * 32
        snapshot = {"lists": {"magic_items": []}, "shop": {"shop_name": "Same"}}
        save_snapshot(token, "game-one", snapshot, db_path=self.db)
        save_snapshot(token, "game-one", snapshot, db_path=self.db, advance_channel=False)
        self.assertEqual(load_snapshot(token, "game-one", db_path=self.db), snapshot)

    def test_generation_request_key_can_own_only_one_snapshot(self):
        generation_key = "single-generation-request"
        save_snapshot(
            "a" * 32,
            "game-one",
            {"version": 1},
            generation_key=generation_key,
            db_path=self.db,
        )

        self.assertEqual(
            generation_request_snapshot(generation_key, db_path=self.db),
            {"token": "a" * 32, "channel": "game-one"},
        )
        with self.assertRaises(DuplicateGeneration) as duplicate:
            save_snapshot(
                "b" * 32,
                "game-one",
                {"version": 2},
                generation_key=generation_key,
                db_path=self.db,
            )
        self.assertEqual(duplicate.exception.token, "a" * 32)
        self.assertEqual(snapshot_count(channel="game-one", db_path=self.db), 1)

    def test_concurrent_workers_cannot_duplicate_a_generation_request(self):
        initialize(self.db)
        barrier = threading.Barrier(2)

        def attempt(token: str) -> str:
            barrier.wait()
            try:
                save_snapshot(
                    token * 32,
                    "game-one",
                    {"worker": token},
                    generation_key="concurrent-generation-request",
                    db_path=self.db,
                )
                return "saved"
            except DuplicateGeneration:
                return "duplicate"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ("a", "b")))

        self.assertEqual(sorted(outcomes), ["duplicate", "saved"])
        self.assertEqual(snapshot_count(channel="game-one", db_path=self.db), 1)

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

    def test_live_token_is_stable_and_tracks_newest_snapshot(self):
        live_token = save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        next_live_token = save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)

        self.assertEqual(live_token, next_live_token)
        self.assertEqual(
            live_channel(live_token, db_path=self.db),
            {"channel": "game-one", "roll_id": "b" * 32},
        )

    def test_live_token_rotation_invalidates_only_the_old_link(self):
        old_token = save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        new_token = rotate_live_token("game-one", db_path=self.db)

        self.assertNotEqual(new_token, old_token)
        with self.assertRaises(LiveChannelNotFound):
            live_channel(old_token, db_path=self.db)
        self.assertEqual(
            live_channel(new_token, db_path=self.db),
            {"channel": "game-one", "roll_id": "a" * 32},
        )

    def test_recent_snapshots_expose_metadata_and_current_status(self):
        save_snapshot(
            "a" * 32,
            "game-one",
            {"shop": {"shop_name": "First Shop", "party_level": 4}, "lists": {}},
            db_path=self.db,
        )
        save_snapshot(
            "b" * 32,
            "game-one",
            {"shop": {"shop_name": "Second Shop", "seed": "repeat-me"}, "lists": {}},
            db_path=self.db,
        )
        save_snapshot("c" * 32, "game-two", {"version": 1}, db_path=self.db)

        rows = recent_snapshots(channel="game-one", db_path=self.db)
        self.assertEqual([row["token"] for row in rows], ["b" * 32, "a" * 32])
        self.assertTrue(rows[0]["is_current"])
        self.assertFalse(rows[1]["is_current"])
        self.assertEqual(rows[0]["shop_name"], "Second Shop")
        self.assertEqual(rows[0]["seed"], "repeat-me")

        paged = recent_snapshots(
            channel="game-one", limit=1, offset=1, db_path=self.db
        )
        self.assertEqual([row["token"] for row in paged], ["a" * 32])
        self.assertEqual(snapshot_count(channel="game-one", db_path=self.db), 2)
        self.assertEqual(snapshot_count(db_path=self.db), 3)
        summaries = {row["channel"]: row for row in channel_summaries(db_path=self.db)}
        self.assertEqual(summaries["game-one"]["snapshots"], 2)
        self.assertEqual(summaries["game-two"]["snapshots"], 1)

    def test_older_snapshot_can_be_restored_to_stable_live_link(self):
        live_token = save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)

        restored_live_token = set_current_snapshot("a" * 32, "game-one", db_path=self.db)

        self.assertEqual(restored_live_token, live_token)
        self.assertEqual(current_token("game-one", db_path=self.db), "a" * 32)
        self.assertEqual(live_channel(live_token, db_path=self.db)["roll_id"], "a" * 32)
        with self.assertRaises(SnapshotNotFound):
            set_current_snapshot("a" * 32, "game-two", db_path=self.db)

    def test_history_page_reopens_and_restores_stored_shop(self):
        previous = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.db)
        try:
            import app
            app.app.config.update(TESTING=True)

            save_snapshot(
                "a" * 32,
                "game-one",
                {"shop": {"shop_name": "Recover Me"}, "lists": {"magic_items": []}},
                db_path=self.db,
            )
            save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)
            client = app.app.test_client()

            page = client.get("/history", query_string={"channel": "game-one"})
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Recover Me", page.data)
            self.assertIn(b"Open GM Results", page.data)
            self.assertIn(b"Open Player View", page.data)
            self.assertIn(b"Rotate Live Link", page.data)
            self.assertIn(b"Download Backup", page.data)
            self.assertIn(b"game-one (2 shops)", page.data)
            self.assertIn(b"Showing 1", page.data)

            downloaded = client.post("/history/backup")
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.headers["Cache-Control"], "no-store")
            self.assertIn(
                "attachment; filename=pf2e-player-views-",
                downloaded.headers["Content-Disposition"],
            )
            downloaded_db = Path(self.tempdir.name) / "downloaded-player-views.db"
            downloaded_db.write_bytes(downloaded.data)
            with closing(sqlite3.connect(downloaded_db)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
            self.assertEqual(
                load_snapshot("a" * 32, "game-one", db_path=downloaded_db)["shop"]["shop_name"],
                "Recover Me",
            )

            restored = client.post(
                "/history/make-live",
                data={"channel": "game-one", "roll_id": "a" * 32},
            )
            self.assertEqual(restored.status_code, 302)
            self.assertEqual(current_token("game-one", db_path=self.db), "a" * 32)

            old_live_token = recent_snapshots(
                channel="game-one", db_path=self.db
            )[0]["live_token"]
            rotated = client.post("/history/rotate-live", data={"channel": "game-one"})
            self.assertEqual(rotated.status_code, 302)
            with self.assertRaises(LiveChannelNotFound):
                live_channel(old_live_token, db_path=self.db)
        finally:
            if previous is None:
                os.environ.pop("LOOTGEN_STATE_DB_PATH", None)
            else:
                os.environ["LOOTGEN_STATE_DB_PATH"] = previous

    def test_opening_old_player_view_does_not_roll_live_channel_back(self):
        save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        live_token = save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)
        save_snapshot(
            "a" * 32,
            "game-one",
            {"version": 1},
            db_path=self.db,
            advance_channel=False,
        )

        self.assertEqual(current_token("game-one", db_path=self.db), "b" * 32)
        self.assertEqual(live_channel(live_token, db_path=self.db)["roll_id"], "b" * 32)

    def test_existing_state_database_is_migrated_for_live_tokens(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                """
                CREATE TABLE player_view_snapshots (
                    token TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE player_view_channels (
                    channel TEXT PRIMARY KEY,
                    current_token TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()
        initialize(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(player_view_channels)")}
            snapshot_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(player_view_snapshots)")
            }
        self.assertIn("live_token", columns)
        self.assertIn("generation_key", snapshot_columns)

    def test_live_route_redirects_and_polls_persistent_state(self):
        previous = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.db)
        try:
            import app
            app.app.config.update(TESTING=True)

            live_token = save_snapshot(
                "a" * 32,
                "game-one",
                {"shop": {"shop_name": "Live"}, "lists": {"magic_items": [{"name": "Wand"}]}},
                db_path=self.db,
            )
            client = app.app.test_client()
            redirect_response = client.get(f"/live/{live_token}")
            self.assertEqual(redirect_response.status_code, 302)
            live_page = client.get(f"/live/{live_token}", follow_redirects=True)
            self.assertEqual(live_page.status_code, 200)
            self.assertIn(b"follows the newest shop", live_page.data)

            version = client.get(f"/api/live/{live_token}/version")
            self.assertEqual(version.status_code, 200)
            self.assertEqual(version.json["roll_id"], "a" * 32)
            self.assertEqual(version.headers["Cache-Control"], "private, no-cache")
            etag = version.headers["ETag"]

            unchanged = client.get(
                f"/api/live/{live_token}/version",
                headers={"If-None-Match": etag},
            )
            self.assertEqual(unchanged.status_code, 304)
            self.assertEqual(unchanged.headers["ETag"], etag)

            save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)
            changed = client.get(
                f"/api/live/{live_token}/version",
                headers={"If-None-Match": etag},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json["roll_id"], "b" * 32)
            self.assertNotEqual(changed.headers["ETag"], etag)
        finally:
            if previous is None:
                os.environ.pop("LOOTGEN_STATE_DB_PATH", None)
            else:
                os.environ["LOOTGEN_STATE_DB_PATH"] = previous

    def test_retention_removes_expired_snapshot_but_protects_current(self):
        save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
        save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE player_view_snapshots SET created_at = '2020-01-01 00:00:00'"
            )
            conn.commit()

        removed = cleanup_snapshots(
            db_path=self.db, retention_days=30, max_snapshots_per_channel=0
        )
        self.assertEqual(removed, 1)
        with self.assertRaises(SnapshotNotFound):
            load_snapshot("a" * 32, "game-one", db_path=self.db)
        self.assertEqual(load_snapshot("b" * 32, "game-one", db_path=self.db), {"version": 2})

    def test_per_channel_limit_does_not_affect_other_games(self):
        for token in ("a", "b", "c", "d"):
            save_snapshot(token * 32, "game-one", {"token": token}, db_path=self.db)
        for token in ("e", "f"):
            save_snapshot(token * 32, "game-two", {"token": token}, db_path=self.db)

        removed = cleanup_snapshots(
            db_path=self.db, retention_days=0, max_snapshots_per_channel=2
        )
        self.assertEqual(removed, 2)
        with self.assertRaises(SnapshotNotFound):
            load_snapshot("a" * 32, "game-one", db_path=self.db)
        with self.assertRaises(SnapshotNotFound):
            load_snapshot("b" * 32, "game-one", db_path=self.db)
        self.assertEqual(load_snapshot("c" * 32, "game-one", db_path=self.db)["token"], "c")
        self.assertEqual(load_snapshot("d" * 32, "game-one", db_path=self.db)["token"], "d")
        self.assertEqual(load_snapshot("e" * 32, "game-two", db_path=self.db)["token"], "e")

    def test_automatic_cleanup_uses_configured_channel_limit(self):
        original = dict(CONFIG.get("player_views", {}))
        try:
            CONFIG["player_views"] = {
                "retention_days": 0,
                "max_snapshots_per_channel": 2,
            }
            save_snapshot("a" * 32, "game-one", {"version": 1}, db_path=self.db)
            save_snapshot("b" * 32, "game-one", {"version": 2}, db_path=self.db)
            save_snapshot("c" * 32, "game-one", {"version": 3}, db_path=self.db)
            with self.assertRaises(SnapshotNotFound):
                load_snapshot("a" * 32, "game-one", db_path=self.db)
            self.assertEqual(snapshot_stats(db_path=self.db)["snapshots"], 2)
        finally:
            CONFIG["player_views"] = original

    def test_missing_shared_view_does_not_regenerate(self):
        previous = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.db)
        try:
            import app
            app.app.config.update(TESTING=True)

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
