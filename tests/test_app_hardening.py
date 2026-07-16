import sqlite3
import unittest
from unittest.mock import patch

import pandas as pd

import app as webapp
import services.player_view_routes as player_view_routes
import services.web_security as web_security
from services.security import AttemptLimiter


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

    def test_history_paginates_retained_shops_and_rejects_invalid_pages(self):
        fake_channels = [
            {
                "channel": "campaign",
                "snapshots": 51,
                "updated_at": "2026-07-15 00:00:00",
                "live_token": "a" * 32,
            }
        ]
        with (
            patch.object(player_view_routes, "snapshot_count", return_value=51),
            patch.object(player_view_routes, "channel_summaries", return_value=fake_channels),
            patch.object(player_view_routes, "recent_snapshots", return_value=[]) as recent,
        ):
            first = self.client.get("/history", query_string={"channel": "campaign"})
            self.assertEqual(first.status_code, 200)
            self.assertIn(b"Page 1 of 2", first.data)
            self.assertIn(b"Older Shops", first.data)
            recent.assert_called_with(channel="campaign", limit=50, offset=0)

            second = self.client.get(
                "/history", query_string={"channel": "campaign", "page": "2"}
            )
            self.assertEqual(second.status_code, 200)
            self.assertIn(b"Page 2 of 2", second.data)
            self.assertIn(b"Newer Shops", second.data)
            recent.assert_called_with(channel="campaign", limit=50, offset=50)

        self.assertEqual(
            self.client.get("/history", query_string={"page": "not-a-page"}).status_code,
            400,
        )

    def test_player_view_is_read_only(self):
        self.assertEqual(self.client.post("/player-view").status_code, 405)
        self.assertEqual(self.client.post("/results/" + "0" * 32).status_code, 405)

    def test_player_view_omits_gm_hidden_items(self):
        snapshot = {
            "shop": {"shop_name": "Visibility Test"},
            "lists": {
                "mundane_items": [
                    {"name": "Visible Rope", "level": 0, "rarity": "Common", "price": "1 sp", "quantity": 1},
                    {"name": "Secret Map", "level": 1, "rarity": "Rare", "price": "10 gp", "quantity": 1, "player_hidden": True},
                ],
                "material_items": [], "formula_items": [], "armor_items": [],
                "weapon_items": [], "magic_items": [],
            },
        }
        with patch.object(player_view_routes, "load_snapshot", return_value=snapshot):
            response = self.client.get(
                "/player-view",
                query_string={"channel": "campaign", "roll_id": "a" * 32},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Visible Rope", response.data)
        self.assertNotIn(b"Secret Map", response.data)

    def test_browser_mutations_require_session_csrf_token(self):
        original = webapp.app.config.get("CSRF_PROTECTION_IN_TESTS")
        webapp.app.config["CSRF_PROTECTION_IN_TESTS"] = True
        try:
            with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": ""}):
                missing = self.client.post("/gm-logout")
                self.assertEqual(missing.status_code, 400)
                self.assertIn(b"form expired", missing.data)

                with self.client.session_transaction() as browser_session:
                    browser_session["csrf_token"] = "known-browser-token"

                wrong = self.client.post(
                    "/gm-logout", data={"csrf_token": "different-token"}
                )
                self.assertEqual(wrong.status_code, 400)

                publish_without_token = self.client.post(
                    "/player-view/publish",
                    data={"channel": "campaign", "roll_id": "0" * 32},
                )
                self.assertEqual(publish_without_token.status_code, 400)

                backup_without_token = self.client.post("/history/backup")
                self.assertEqual(backup_without_token.status_code, 400)

                accepted = self.client.post(
                    "/gm-logout", data={"csrf_token": "known-browser-token"}
                )
                self.assertEqual(accepted.status_code, 302)
                self.assertIn("/gm-login", accepted.headers["Location"])
        finally:
            webapp.app.config["CSRF_PROTECTION_IN_TESTS"] = original

    def test_login_form_contains_csrf_token(self):
        with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
            response = self.client.get("/gm-login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="csrf_token"', response.data)

    def test_optional_gm_access_protects_generator_but_not_player_links(self):
        with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
            root = self.client.get("/")
            self.assertEqual(root.status_code, 302)
            self.assertIn("/gm-login", root.headers["Location"])
            self.assertEqual(self.client.get("/history").status_code, 302)
            self.assertEqual(
                self.client.get(
                    "/results/" + "0" * 32,
                    query_string={"channel": "campaign"},
                ).status_code,
                302,
            )
            self.assertEqual(
                self.client.post(
                    "/history/make-live",
                    data={"channel": "campaign", "roll_id": "0" * 32},
                ).status_code,
                302,
            )
            self.assertEqual(self.client.post("/history/backup").status_code, 302)

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

    def test_repeated_failed_gm_logins_are_throttled_per_client(self):
        original = web_security._login_limiter
        web_security._login_limiter = AttemptLimiter(2, 300)
        try:
            with patch.dict("os.environ", {"LOOTGEN_GM_ACCESS_KEY": "private-table-key"}):
                for _attempt in range(2):
                    response = self.client.post(
                        "/gm-login",
                        data={"access_key": "wrong"},
                        environ_base={"REMOTE_ADDR": "198.51.100.10"},
                    )
                    self.assertEqual(response.status_code, 401)

                blocked = self.client.post(
                    "/gm-login",
                    data={"access_key": "private-table-key"},
                    environ_base={"REMOTE_ADDR": "198.51.100.10"},
                )
                self.assertEqual(blocked.status_code, 429)
                self.assertIn(b"Too many attempts", blocked.data)

                other_client = self.client.post(
                    "/gm-login",
                    data={"access_key": "wrong"},
                    environ_base={"REMOTE_ADDR": "198.51.100.11"},
                )
                self.assertEqual(other_client.status_code, 401)
        finally:
            web_security._login_limiter = original

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
