import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ApplicationStructureTests(unittest.TestCase):
    def test_generation_and_magic_builder_are_not_defined_in_app_module(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("def _st_norm(", app_source)
        self.assertNotIn('def api_mib_bases(', app_source)
        self.assertNotIn('def api_mib_build(', app_source)
        self.assertIn("generate_shop_snapshot", app_source)
        self.assertIn("magic_builder_bp", app_source)

    def test_normalization_and_shield_helpers_have_one_definition_each(self):
        builder_source = (ROOT / "services" / "magic_builder.py").read_text(
            encoding="utf-8"
        )
        logic_source = (ROOT / "services" / "logic.py").read_text(encoding="utf-8")
        self.assertEqual(builder_source.count("def _st_norm("), 1)
        self.assertEqual(logic_source.count("def _is_shield("), 1)


if __name__ == "__main__":
    unittest.main()
