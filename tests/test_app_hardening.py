import sqlite3
import unittest
from unittest.mock import patch

import pandas as pd

import app as webapp


class AppHardeningTests(unittest.TestCase):
    def setUp(self):
        webapp.app.config.update(
            TESTING=True,
            SECRET_KEY="test-session-secret",
            SESSION_COOKIE_SECURE=False,
        )
        self.client = webapp.app.test_client()

    def test_debug_routes_are_disabled_by_default(self):
        self.assertEqual(self.client.get("/debug/health").status_code, 404)

    def test_legacy_channel_endpoints_do_not_expose_snapshot_tokens(self):
        self.assertEqual(self.client.get("/version", query_string={"channel": "default"}).status_code, 404)
        self.assertEqual(self.client.get("/events", query_string={"channel": "default"}).status_code, 404)

    def test_health_reports_ready_dependencies(self):
        with (
            patch.object(webapp, "load_items", return_value=pd.DataFrame([{"name": "item"}])),
            patch.object(webapp, "initialize_player_views"),
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json,
            {"ok": True, "checks": {"catalog": True, "player_view_storage": True}},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_health_returns_503_when_storage_is_unavailable(self):
        with self.assertLogs(webapp.app.logger, level="WARNING") as logs:
            with (
                patch.object(webapp, "load_items", return_value=pd.DataFrame([{"name": "item"}])),
                patch.object(webapp, "initialize_player_views", side_effect=sqlite3.Error("offline")),
            ):
                response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])
        self.assertFalse(response.json["checks"]["player_view_storage"])
        self.assertIn("offline", logs.output[0])
        self.assertNotIn("Traceback", "\n".join(logs.output))

    def test_html_errors_are_readable_and_do_not_show_internal_details(self):
        missing = self.client.get("/not-a-real-page")
        self.assertEqual(missing.status_code, 404)
        self.assertIn(b"Page not found", missing.data)
        self.assertIn(b"Return to the generator", missing.data)

        with self.assertLogs(webapp.app.logger, level="ERROR"):
            with patch.object(webapp, "load_items", side_effect=RuntimeError("secret detail")):
                failed = self.client.get("/")
        self.assertEqual(failed.status_code, 500)
        self.assertIn(b"Something went wrong", failed.data)
        self.assertNotIn(b"secret detail", failed.data)

    def test_api_errors_remain_machine_readable(self):
        response = self.client.get("/api/not-a-real-route")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json["ok"])
        self.assertIn("error", response.json)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_oversized_api_request_returns_json_413(self):
        original_limit = webapp.app.config["MAX_CONTENT_LENGTH"]
        webapp.app.config["MAX_CONTENT_LENGTH"] = 100
        try:
            response = self.client.post(
                "/api/spellbooks/generate",
                json={"tradition": "Arcane", "themes": "x" * 500},
            )
        finally:
            webapp.app.config["MAX_CONTENT_LENGTH"] = original_limit
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json["ok"])

    def test_query_rejects_unknown_generation_options(self):
        catalog = pd.DataFrame([{"shop_type": "General"}])
        invalid_cases = (
            {"shop_type": "Unknown"},
            {"shop_type": "General", "shop_size": "enormous"},
            {"shop_type": "General", "disposition": "hostile"},
            {"shop_type": "General", "party_level": "21"},
            {"shop_type": "General", "party_level": "five"},
        )
        with patch.object(webapp, "load_items", return_value=catalog):
            for query in invalid_cases:
                with self.subTest(query=query):
                    self.assertEqual(self.client.post("/query", data=query).status_code, 400)

    def test_query_generation_is_post_only(self):
        self.assertEqual(self.client.get("/query").status_code, 405)

    def test_optional_gm_access_protects_generator_but_not_player_links(self):
        with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
            root = self.client.get("/")
            self.assertEqual(root.status_code, 302)
            self.assertIn("/gm-login", root.headers["Location"])
            self.assertEqual(self.client.get("/history").status_code, 302)
            self.assertEqual(
                self.client.post(
                    "/history/make-live",
                    data={"channel": "campaign", "roll_id": "0" * 32},
                ).status_code,
                302,
            )

            api = self.client.get("/api/magic-builder/bases", query_string={"type": "weapon"})
            self.assertEqual(api.status_code, 401)
            self.assertFalse(api.json["ok"])

            with (
                patch.object(webapp, "load_items", return_value=pd.DataFrame([{"name": "item"}])),
                patch.object(webapp, "initialize_player_views"),
            ):
                self.assertEqual(self.client.get("/health").status_code, 200)

            missing_snapshot = self.client.get(
                "/player-view",
                query_string={"channel": "campaign", "roll_id": "0" * 32},
            )
            self.assertEqual(missing_snapshot.status_code, 404)

    def test_gm_login_rejects_wrong_key_and_logout_locks_generator(self):
        with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
            rejected = self.client.post(
                "/gm-login", data={"access_key": "wrong", "next": "/not-a-real-page"}
            )
            self.assertEqual(rejected.status_code, 401)
            self.assertIn(b"not accepted", rejected.data)

            accepted = self.client.post(
                "/gm-login", data={"access_key": "private-table-key", "next": "/not-a-real-page"}
            )
            self.assertEqual(accepted.status_code, 302)
            self.assertTrue(accepted.headers["Location"].endswith("/not-a-real-page"))
            self.assertEqual(self.client.get("/not-a-real-page").status_code, 404)

            logged_out = self.client.post("/gm-logout")
            self.assertEqual(logged_out.status_code, 302)
            self.assertIn("/gm-login", logged_out.headers["Location"])
            self.assertEqual(self.client.get("/").status_code, 302)

    def test_gm_login_does_not_redirect_to_an_external_site(self):
        with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
            response = self.client.post(
                "/gm-login",
                data={"access_key": "private-table-key", "next": "//example.com/steal"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))

    def test_magic_builder_rejects_out_of_range_level_before_loading_catalog(self):
        response = self.client.get(
            "/api/magic-builder/bases", query_string={"type": "weapon", "max_level": 99}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/magic-builder/build",
            json={"item_type": "weapon", "base_name": "Club", "max_level": 99},
        )
        self.assertEqual(response.status_code, 400)

    def test_spellbook_view_rejects_invalid_tradition(self):
        self.assertEqual(
            self.client.get("/spellbooks/view", query_string={"tradition": "Unknown"}).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
