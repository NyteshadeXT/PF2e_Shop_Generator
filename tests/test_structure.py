import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ApplicationStructureTests(unittest.TestCase):
    def test_generation_and_magic_builder_are_not_defined_in_app_module(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        logic_source = (ROOT / "services" / "logic.py").read_text(encoding="utf-8")
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
        rune_prerequisites_source = (
            ROOT / "services" / "rune_prerequisites.py"
        ).read_text(encoding="utf-8")
        rune_selection_source = (
            ROOT / "services" / "rune_selection.py"
        ).read_text(encoding="utf-8")
        runed_equipment_source = (
            ROOT / "services" / "runed_equipment.py"
        ).read_text(encoding="utf-8")
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
        self.assertNotIn("_SPELLS_DF_CACHE:", logic_source)
        self.assertNotIn("def build_payload(", (ROOT / "services" / "generation.py").read_text(encoding="utf-8"))
        self.assertIn("def collect_item_context(", rune_prerequisites_source)
        self.assertIn("def prerequisites_match(", rune_prerequisites_source)
        self.assertNotIn("def _prerequisites_match(", logic_source)
        self.assertIn("def weapon_fundamental_candidates(", rune_selection_source)
        self.assertIn("def armor_fundamental_candidates(", rune_selection_source)
        self.assertIn("def weapon_property_candidates(", rune_selection_source)
        self.assertIn("def armor_property_candidates(", rune_selection_source)
        self.assertIn("from services.rune_selection import (", logic_source)
        self.assertIn("def apply_weapon_runes(", runed_equipment_source)
        self.assertIn("def apply_armor_runes(", runed_equipment_source)
        self.assertIn("def apply_shield_runes(", runed_equipment_source)
        self.assertIn("def compose_weapon_name(", runed_equipment_source)
        self.assertIn("from services.runed_equipment import (", logic_source)
        for function_name in (
            "apply_weapon_runes",
            "apply_armor_runes",
            "apply_shield_runes",
            "_compose_weapon_name",
            "_compose_armor_name",
        ):
            self.assertNotIn(f"def {function_name}(", logic_source)
        for function_name in (
            "apply_weapon_runes",
            "apply_armor_runes",
            "apply_shield_runes",
            "compose_weapon_name",
            "compose_armor_name",
        ):
            self.assertEqual(runed_equipment_source.count(f"def {function_name}("), 1)

    def test_normalization_and_shield_helpers_have_one_definition_each(self):
        builder_source = (ROOT / "services" / "magic_builder.py").read_text(
            encoding="utf-8"
        )
        logic_source = (ROOT / "services" / "logic.py").read_text(encoding="utf-8")
        rune_selection_source = (ROOT / "services" / "rune_selection.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(builder_source.count("def _st_norm("), 1)
        self.assertEqual(logic_source.count("def _is_shield("), 0)
        self.assertEqual(rune_selection_source.count("def is_shield("), 1)


if __name__ == "__main__":
    unittest.main()
