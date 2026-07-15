import subprocess
import sys
import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.security import AttemptLimiter, SQLiteAttemptLimiter, load_session_secret


class AttemptLimiterTests(unittest.TestCase):
    def test_failures_expire_and_success_can_clear_them(self):
        now = [100.0]
        limiter = AttemptLimiter(2, 10, clock=lambda: now[0])

        limiter.record_failure("client")
        self.assertFalse(limiter.blocked("client"))
        limiter.record_failure("client")
        self.assertTrue(limiter.blocked("client"))

        now[0] = 111.0
        self.assertFalse(limiter.blocked("client"))
        limiter.record_failure("client")
        limiter.clear("client")
        self.assertFalse(limiter.blocked("client"))

    def test_sqlite_limit_is_shared_by_independent_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            first = SQLiteAttemptLimiter(2, 60, path, clock=lambda: 100.0)
            second = SQLiteAttemptLimiter(2, 60, path, clock=lambda: 100.0)

            first.record_failure("198.51.100.10")
            self.assertFalse(second.blocked("198.51.100.10"))
            second.record_failure("198.51.100.10")
            self.assertTrue(first.blocked("198.51.100.10"))

            second.clear("198.51.100.10")
            self.assertFalse(first.blocked("198.51.100.10"))

    def test_sqlite_limit_serializes_concurrent_worker_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            limiters = [SQLiteAttemptLimiter(8, 60, path) for _worker in range(8)]
            with ThreadPoolExecutor(max_workers=8) as workers:
                list(
                    workers.map(
                        lambda limiter: limiter.record_failure("203.0.113.7"),
                        limiters,
                    )
                )
            self.assertTrue(limiters[0].blocked("203.0.113.7"))

    def test_sqlite_limit_is_shared_by_separate_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            command = (
                sys.executable,
                "-c",
                "import sys; from services.security import SQLiteAttemptLimiter; "
                "SQLiteAttemptLimiter(4, 60, sys.argv[1]).record_failure('client')",
                str(path),
            )
            processes = [subprocess.Popen(command) for _worker in range(4)]
            for process in processes:
                self.assertEqual(process.wait(timeout=10), 0)

            self.assertTrue(SQLiteAttemptLimiter(4, 60, path).blocked("client"))

    def test_sqlite_limit_expires_attempts_and_does_not_store_raw_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            now = [100.0]
            limiter = SQLiteAttemptLimiter(1, 10, path, clock=lambda: now[0])
            limiter.record_failure("sensitive-client-address")
            self.assertTrue(limiter.blocked("sensitive-client-address"))

            connection = sqlite3.connect(path)
            try:
                saved_key = connection.execute(
                    "SELECT client_key FROM gm_login_failures"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertNotIn("sensitive-client-address", saved_key)

            now[0] = 111.0
            self.assertFalse(limiter.blocked("sensitive-client-address"))

    def test_sqlite_limit_recovers_after_state_database_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            limiter = SQLiteAttemptLimiter(2, 60, path)
            limiter.record_failure("client")
            path.unlink()
            sqlite3.connect(path).close()

            limiter.record_failure("client")
            self.assertFalse(limiter.blocked("client"))


class SessionSecretTests(unittest.TestCase):
    def test_explicit_secret_is_preferred_without_creating_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lootgen-session-secret"

            secret = load_session_secret(" configured-secret ", path)

            self.assertEqual(secret, "configured-secret")
            self.assertFalse(path.exists())

    def test_fallback_secret_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / ".lootgen-session-secret"

            first = load_session_secret(None, path)
            second = load_session_secret("", path)

            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(second, first)
            self.assertEqual(path.read_text(encoding="utf-8").strip(), first)

    def test_concurrent_workers_receive_the_same_fallback_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lootgen-session-secret"
            with ThreadPoolExecutor(max_workers=8) as workers:
                secrets = list(
                    workers.map(lambda _worker: load_session_secret(None, path), range(8))
                )

            self.assertEqual(len(set(secrets)), 1)

    def test_separate_processes_receive_the_same_fallback_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lootgen-session-secret"
            command = (
                sys.executable,
                "-c",
                "import sys; from services.security import load_session_secret; "
                "print(load_session_secret(None, sys.argv[1]))",
                str(path),
            )
            processes = [
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _worker in range(6)
            ]
            results = [process.communicate(timeout=10) for process in processes]

            for process, (_stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(len({stdout.strip() for stdout, _stderr in results}), 1)

    def test_invalid_existing_fallback_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lootgen-session-secret"
            path.write_text("too-short\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                load_session_secret(None, path, wait_attempts=1)


if __name__ == "__main__":
    unittest.main()
