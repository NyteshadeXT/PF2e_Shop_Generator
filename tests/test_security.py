import unittest

from services.security import AttemptLimiter


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


if __name__ == "__main__":
    unittest.main()
