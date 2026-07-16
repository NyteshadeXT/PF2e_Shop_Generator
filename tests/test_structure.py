import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ApplicationStructureTests(unittest.TestCase):
    def test_generation_and_magic_builder_are_not_defined_in_app_module(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        player_routes_source = (ROOT / "services" / "player_view_routes.py").read_text(
            encoding="utf-8"
        )
        security_source = (ROOT / "services" / "web_security.py").read_text(
            encoding="utf-8"
        )
        sections_source = (ROOT / "services" / "inventory_sections.py").read_text(
            encoding="utf-8"
        )
        spell_items_source = (ROOT / "services" / "spell_items.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("def _st_norm(", app_source)
        self.assertNotIn('def api_mib_bases(', app_source)
        self.assertNotIn('def api_mib_build(', app_source)
        self.assertIn("generate_shop_snapshot", app_source)
        self.assertIn("magic_builder_bp", app_source)
        self.assertIn("register_player_view_routes(app)", app_source)
        self.assertNotIn('def player_view(', app_source)
        self.assertNotIn('def history(', app_source)
        self.assertIn('def player_view(', player_routes_source)
        self.assertIn('def history(', player_routes_source)
        self.assertIn("configure_web_security(", app_source)
        self.assertNotIn('def gm_login(', app_source)
        self.assertIn('def gm_login(', security_source)
        self.assertIn("INVENTORY_SECTIONS = (", sections_source)
        self.assertIn("SECTION_LIST_KEYS", sections_source)
        self.assertIn("def enrich_spell_scrolls(", spell_items_source)
        self.assertIn("def enrich_magic_wands(", spell_items_source)
        self.assertNotIn("_SPELLS_DF_CACHE:", app_source)
        self.assertNotIn("_SPELLS_DF_CACHE:", (ROOT / "services" / "logic.py").read_text(encoding="utf-8"))
        self.assertNotIn("def build_payload(", (ROOT / "services" / "generation.py").read_text(encoding="utf-8"))

    def test_normalization_and_shield_helpers_have_one_definition_each(self):
        builder_source = (ROOT / "services" / "magic_builder.py").read_text(
            encoding="utf-8"
        )
        logic_source = (ROOT / "services" / "logic.py").read_text(encoding="utf-8")
        self.assertEqual(builder_source.count("def _st_norm("), 1)
        self.assertEqual(logic_source.count("def _is_shield("), 1)


if __name__ == "__main__":
    unittest.main()
