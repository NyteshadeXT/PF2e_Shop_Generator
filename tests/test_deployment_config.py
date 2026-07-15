import json
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests import production_smoke


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
        with sqlite3.connect(catalog_path) as connection:
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
        self.assertIn('RENDER: "1"', workflow)

    def test_production_server_dependency_is_declared(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("gunicorn", requirements.lower())

    def test_smoke_client_validates_the_expected_http_contract(self):
        class SmokeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    body = json.dumps(
                        {
                            "ok": True,
                            "checks": {
                                "catalog": True,
                                "player_view_storage": True,
                            },
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                elif self.path == "/":
                    body = b"redirect"
                    self.send_response(302)
                    self.send_header("Location", "/gm-login")
                elif self.path == "/gm-login":
                    body = b"<h1>GM Access</h1>"
                    self.send_response(200)
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self' 'nonce-test'; "
                        "object-src 'none'",
                    )
                elif self.path == "/static/pf2e.css":
                    body = b"body { color: black; }"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/css")
                elif self.path.startswith("/player-view?"):
                    body = b"This Player View is no longer available."
                    self.send_response(404)
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), SmokeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            production_smoke.run(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
