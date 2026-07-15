import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.player_views import load_snapshot
from services.randomness import generation_rng, get_rng, normalize_seed


class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self.tempdir.name) / "views.db"
        self.previous_state_path = os.environ.get("LOOTGEN_STATE_DB_PATH")
        os.environ["LOOTGEN_STATE_DB_PATH"] = str(self.state_db)
        import app

        self.app_module = app
        self.client = app.app.test_client()

    def tearDown(self):
        if self.previous_state_path is None:
            os.environ.pop("LOOTGEN_STATE_DB_PATH", None)
        else:
            os.environ["LOOTGEN_STATE_DB_PATH"] = self.previous_state_path
        self.tempdir.cleanup()

    def _generate(self, seed: str, channel: str = "seed-test"):
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
            },
        )
        self.assertEqual(response.status_code, 200)
        token = self.client.get(f"/version?channel={channel}").json["roll_id"]
        return response, load_snapshot(token, channel, db_path=self.state_db)

    def test_same_seed_produces_identical_complete_snapshot(self):
        first_response, first = self._generate("repeatable-shop")
        second_response, second = self._generate("repeatable-shop")

        self.assertEqual(first, second)
        self.assertIn(b"repeatable-shop", first_response.data)
        self.assertIn(b"Recreate Same Seed", second_response.data)

    def test_different_seed_changes_inventory(self):
        _, first = self._generate("seed-one")
        _, second = self._generate("seed-two")
        self.assertNotEqual(first["lists"], second["lists"])

    def test_blank_seed_creates_shareable_seed(self):
        first = normalize_seed("")
        second = normalize_seed(None)
        self.assertEqual(len(first), 16)
        self.assertNotEqual(first, second)

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
