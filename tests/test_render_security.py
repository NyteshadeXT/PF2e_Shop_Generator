import unittest
from pathlib import Path

from flask import render_template

import app as webapp
from services.spellbooks import _render_contents_html


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RenderSecurityTests(unittest.TestCase):
    def setUp(self):
        webapp.app.config.update(TESTING=True)
        self.client = webapp.app.test_client()

    def test_spellbook_html_escapes_database_fields(self):
        rendered = _render_contents_html({
            1: [{
                "name": '<img src=x onerror="alert(1)">',
                "source": '</span><script>alert(2)</script>',
            }]
        })
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("&lt;/span&gt;&lt;script&gt;", rendered)

    def test_shop_name_is_json_encoded_inside_script(self):
        attack = '</script><script>alert("shop")</script>'
        with webapp.app.test_request_context("/"):
            rendered = render_template(
                "results.html",
                shop_name=attack,
                shop_type="General",
                shop_size="small",
                disposition="fair",
                party_level=5,
                seed="safe-seed",
                channel="default",
                roll_id="a" * 32,
                live_token="",
                picked={},
                counts={},
                mundane_items=[],
                material_items=[],
                armor_items=[],
                weapon_items=[],
                magic_items=[],
                formula_items=[],
            )
        self.assertNotIn(attack, rendered)
        self.assertIn("\\u003c/script\\u003e", rendered)

    def test_magic_builder_uses_text_content_for_api_fields(self):
        template = (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("$out.innerHTML", template)
        self.assertIn("title.textContent = it.name", template)
        self.assertIn("error.textContent", template)

    def test_csv_export_has_been_removed(self):
        for name in ("results.html", "results_player.html"):
            template = (PROJECT_ROOT / "templates" / name).read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertNotIn("exportToCSV", template)
                self.assertNotIn("treasure_results.csv", template)

    def test_privacy_and_content_headers_are_applied(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("geolocation=()", response.headers["Permissions-Policy"])


if __name__ == "__main__":
    unittest.main()
