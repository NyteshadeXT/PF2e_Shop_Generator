import json
import sqlite3
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path

from tests import production_smoke
from services.catalog_validation import validate_catalog


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DeploymentConfigurationTests(unittest.TestCase):
    def test_required_deployment_artifacts_are_present_and_readable(self):
        config_path = PROJECT_ROOT / "config.json"
        catalog_path = PROJECT_ROOT / "data" / "PF2e_Treasure_Generator_Backend.db"

        self.assertTrue(config_path.is_file(), "config.json must be committed to GitHub")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["data_source"], "sqlite")
        self.assertEqual(
            config["sqlite_db_path"],
            "data/PF2e_Treasure_Generator_Backend.db",
        )

        self.assertTrue(catalog_path.is_file(), "the SQLite catalog must be deployed")
        self.assertGreater(catalog_path.stat().st_size, 1_000_000)
        report = validate_catalog(catalog_path, config["sqlite_view"])
        self.assertGreater(report["rows"], 8_000)
        self.assertGreaterEqual(report["sources"], 20)
        with closing(sqlite3.connect(catalog_path)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            view = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'view' AND name = ?
                """,
                (config["sqlite_view"],),
            ).fetchone()
        self.assertEqual(integrity, "ok")
        self.assertIsNotNone(view)

    def test_ci_starts_the_render_server_and_runs_http_smoke_checks(self):
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("gunicorn --bind 127.0.0.1:8765 --workers 2", workflow)
        self.assertIn("tests/production_smoke.py", workflow)
        self.assertIn("LOOTGEN_STATE_DB_PATH:", workflow)
        self.assertIn('LOOTGEN_LOGIN_ATTEMPTS: "2"', workflow)
        self.assertIn('LOOTGEN_LOGIN_WINDOW_SECONDS: "1"', workflow)
        self.assertIn('RENDER: "1"', workflow)

    def test_production_server_dependency_is_declared(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertEqual(
            requirements.splitlines(),
            [
                "Flask==3.1.3",
                "gunicorn==23.0.0",
                "pandas==2.3.3",
                "numpy==2.3.4",
            ],
        )

    def test_render_and_ci_share_the_python_runtime_file(self):
        runtime = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(runtime, "3.13")
        self.assertIn('python-version-file: ".python-version"', workflow)

    def test_windows_launcher_repairs_partial_environment_and_checks_assets(self):
        launcher = (PROJECT_ROOT / "run_app.bat").read_text(encoding="utf-8")

        self.assertIn('if not exist "config.json"', launcher)
        self.assertIn(
            'if not exist "data\\PF2e_Treasure_Generator_Backend.db"',
            launcher,
        )
        self.assertIn('if not exist "%VENV_PYTHON%"', launcher)
        self.assertIn('set /p "REQUIRED_PYTHON="<.python-version', launcher)
        self.assertIn('set "REBUILD_VENV=1"', launcher)
        self.assertIn("Existing virtual environment uses the wrong Python version", launcher)
        self.assertIn("Python %REQUIRED_PYTHON% is not installed", launcher)
        self.assertIn("py -%REQUIRED_PYTHON% -m venv --clear .venv", launcher)
        self.assertIn("validate_catalog", launcher)
        self.assertIn("python -m pip install --prefer-binary -r requirements.txt", launcher)
        self.assertIn(":startup_failed", launcher)
        self.assertIn("pause", launcher)

    def test_local_runtime_files_are_ignored_but_deploy_assets_are_not(self):
        ignore_rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn(".venv/", ignore_rules)
        self.assertIn("data/player_views.db", ignore_rules)
        self.assertIn(".lootgen-session-secret", ignore_rules)
        self.assertNotIn("config.json", ignore_rules)
        self.assertNotIn("PF2e_Treasure_Generator_Backend.db", ignore_rules)

    def test_csv_catalog_fallback_code_has_been_removed(self):
        sources = "\n".join(
            (PROJECT_ROOT / path).read_text(encoding="utf-8")
            for path in (
                "services/db.py",
                "services/settings.py",
                "services/provenance.py",
            )
        )

        self.assertNotIn("read_csv", sources)
        self.assertNotIn("LOOTGEN_CSV_PATH", sources)
        self.assertNotIn("csv_adjustments_path", sources)

    def test_smoke_client_parses_authenticated_workflow_contract(self):
        body = (
            b'<input type="hidden" name="csrf_token" value="csrf-123">'
            b'<a href="/live/0123456789abcdef0123456789abcdef">Live</a>'
        )
        headers = Message()
        headers["Location"] = (
            "/results/abcdefabcdefabcdefabcdefabcdefab?channel=friday-game"
        )

        self.assertEqual(production_smoke._hidden_value(body, "csrf_token"), "csrf-123")
        self.assertEqual(
            production_smoke._result_location(headers),
            (
                "/results/abcdefabcdefabcdefabcdefabcdefab?channel=friday-game",
                "abcdefabcdefabcdefabcdefabcdefab",
                "friday-game",
            ),
        )
        self.assertEqual(
            production_smoke._live_token(body),
            "0123456789abcdef0123456789abcdef",
        )


if __name__ == "__main__":
    unittest.main()
