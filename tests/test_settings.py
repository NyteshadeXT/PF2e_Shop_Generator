import json
import tempfile
import unittest
from pathlib import Path

from services.settings import ConfigurationError, load_settings


class SettingsTests(unittest.TestCase):
    def _config_copy(self, directory: Path) -> Path:
        source = Path(__file__).resolve().parent.parent / "config.json"
        config = json.loads(source.read_text(encoding="utf-8"))
        target = directory / "config.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        return target

    def test_database_environment_override_is_honored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config_copy(root)
            overridden = root / "custom.db"
            settings = load_settings(
                config_path,
                env={"LOOTGEN_DB_PATH": str(overridden)},
            )
            self.assertEqual(settings["sqlite_db_path"], str(overridden.resolve()))

    def test_relative_paths_resolve_from_config_location(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config_copy(root)
            settings = load_settings(config_path, env={})
            self.assertEqual(
                settings["sqlite_db_path"],
                str((root / "data/PF2e_Treasure_Generator_Backend.db").resolve()),
            )

    def test_invalid_count_band_fails_at_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config_copy(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["counts"]["small"]["magic"] = [10, 2]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_settings(config_path, env={})

    def test_unsafe_sql_view_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config_copy(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["sqlite_view"] = "v_items_norm; DROP TABLE items"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_settings(config_path, env={})


if __name__ == "__main__":
    unittest.main()
