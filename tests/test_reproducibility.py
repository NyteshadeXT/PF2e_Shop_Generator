import os
import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.player_views import current_token, load_snapshot, recent_snapshots, snapshot_count
from services.randomness import generation_rng, get_rng, normalize_seed
from services.reproduction import create_reproduction_key, parse_reproduction_key


class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self.tempdir.name) / "views.db"
        self.previous_state_path = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.state_db)
        import app

        self.app_module = app
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()
        self.request_number = 0

    def tearDown(self):
        if self.previous_state_path is None:
            os.environ.pop("LOOTGEN_STATE_DB_PATH", None)
        else:
            os.environ["LOOTGEN_STATE_DB_PATH"] = self.previous_state_path
        self.tempdir.cleanup()

    def _generate(self, seed: str, channel: str = "seed-test"):
        self.request_number += 1
        response = self.client.post(
            "/query",
            data={
                "shop_type": "General",
                "shop_size": "small",
                "disposition": "fair",
                "party_level": "5",
                "shop_name": "Seed Test",
                "channel": channel,
                "seed": seed,
                "generation_request_key": f"test-request-{self.request_number:08d}",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        token = recent_snapshots(channel=channel, limit=1, db_path=self.state_db)[0]["token"]
        return response, load_snapshot(token, channel, db_path=self.state_db)

    def test_same_seed_produces_identical_complete_snapshot(self):
        first_response, first = self._generate("repeatable-shop")
        second_response, second = self._generate("repeatable-shop")

        self.assertEqual(first, second)
        self.assertIn(b"repeatable-shop", first_response.data)
        self.assertIn(b"Recreate Same Seed", second_response.data)
        self.assertIn(b"Copy Reproduction Key", second_response.data)
        self.assertNotIn(b"Export All to CSV", second_response.data)

    def test_different_seed_changes_inventory(self):
        _, first = self._generate("seed-one")
        _, second = self._generate("seed-two")
        self.assertNotEqual(first["lists"], second["lists"])

    def test_blank_seed_creates_shareable_seed(self):
        first = normalize_seed("")
        second = normalize_seed(None)
        self.assertEqual(len(first), 16)
        self.assertNotEqual(first, second)

    def test_reproduction_key_round_trip(self):
        key = create_reproduction_key(
            seed="portable-shop",
            shop_type="General",
            shop_size="small",
            disposition="fair",
            party_level=5,
            fingerprint="0123456789abcdef",
        )
        self.assertEqual(
            parse_reproduction_key(key),
            {
                "seed": "portable-shop",
                "shop_type": "General",
                "shop_size": "small",
                "disposition": "fair",
                "party_level": 5,
                "_generation_fingerprint": "0123456789abcdef",
            },
        )

    def test_legacy_reproduction_key_remains_accepted_with_unknown_build(self):
        payload = {"d": "fair", "l": 5, "s": "legacy-shop", "t": "General", "z": "small"}
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii").rstrip("=")
        parsed = parse_reproduction_key("pf2e1." + encoded)
        self.assertEqual(parsed["seed"], "legacy-shop")
        self.assertEqual(parsed["_generation_fingerprint"], "")

    def test_different_build_key_warns_and_records_current_fingerprint(self):
        key = create_reproduction_key(
            seed="older-build",
            shop_type="General",
            shop_size="small",
            disposition="fair",
            party_level=5,
            fingerprint="different-build",
        )
        response = self.client.post(
            "/query",
            data={
                "seed": key,
                "channel": "seed-test",
                "shop_name": "Compatibility Test",
                "generation_request_key": "different-build-request",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"different catalog or generator build", response.data)
        token = recent_snapshots(
            channel="seed-test", limit=1, db_path=self.state_db
        )[0]["token"]
        snapshot = load_snapshot(token, "seed-test", db_path=self.state_db)
        self.assertNotEqual(snapshot["shop"]["generation_fingerprint"], "different-build")

    def test_reproduction_key_restores_seed_and_settings(self):
        _, original = self._generate("portable-shop")
        key = original["shop"]["reproduction_key"]
        response = self.client.post(
            "/query",
            data={
                "shop_type": "Arcane",
                "shop_size": "grand",
                "disposition": "greedy",
                "party_level": "20",
                "shop_name": "Restored from Key",
                "channel": "seed-test",
                "seed": key,
                "generation_request_key": "restore-settings-request",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        token = recent_snapshots(
            channel="seed-test", limit=1, db_path=self.state_db
        )[0]["token"]
        restored = load_snapshot(token, "seed-test", db_path=self.state_db)
        self.assertEqual(restored["lists"], original["lists"])
        self.assertEqual(restored["shop"]["seed"], "portable-shop")
        for setting in ("shop_type", "shop_size", "disposition", "party_level"):
            self.assertEqual(restored["shop"][setting], original["shop"][setting])

    def test_later_generation_is_a_draft_until_explicitly_published(self):
        first_response, _first = self._generate("published-shop")
        first_token = current_token("seed-test", db_path=self.state_db)
        self.assertIn(b"Live now", first_response.data)

        draft_response, draft = self._generate("draft-shop")
        draft_token = recent_snapshots(
            channel="seed-test", limit=1, db_path=self.state_db
        )[0]["token"]
        self.assertNotEqual(draft_token, first_token)
        self.assertEqual(current_token("seed-test", db_path=self.state_db), first_token)
        self.assertIn(b"Publish to Live Display", draft_response.data)
        self.assertIn(b"players still see the previous live shop", draft_response.data)

        published = self.client.post(
            "/player-view/publish",
            data={"channel": "seed-test", "roll_id": draft_token},
        )
        self.assertEqual(published.status_code, 302)
        self.assertIn("/player-view?", published.headers["Location"])
        self.assertEqual(current_token("seed-test", db_path=self.state_db), draft_token)
        self.assertEqual(
            load_snapshot(draft_token, "seed-test", db_path=self.state_db), draft
        )

    def test_invalid_reproduction_key_is_rejected(self):
        response = self.client.post(
            "/query",
            data={"seed": "pf2e1.not-valid", "channel": "seed-test"},
        )
        self.assertEqual(response.status_code, 400)

    def test_generation_post_redirects_and_replay_reuses_one_snapshot(self):
        payload = {
            "shop_type": "General",
            "shop_size": "small",
            "disposition": "fair",
            "party_level": "5",
            "shop_name": "One Request",
            "channel": "idempotent-game",
            "seed": "one-result",
            "generation_request_key": "idempotent-request-0001",
        }

        first = self.client.post("/query", data=payload)
        second = self.client.post("/query", data=payload)

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers["Location"], first.headers["Location"])
        self.assertEqual(
            snapshot_count(channel="idempotent-game", db_path=self.state_db), 1
        )
        result = self.client.get(first.headers["Location"])
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"One Request", result.data)

    def test_request_local_rngs_do_not_interfere(self):
        def sequence(seed):
            with generation_rng(seed):
                return [get_rng().randint(0, 10**9) for _ in range(100)]

        expected = sequence("concurrent")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(sequence, ["concurrent"] * 8))
        self.assertTrue(all(result == expected for result in results))


if __name__ == "__main__":
    unittest.main()
