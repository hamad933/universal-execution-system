import unittest

from ues.providers.base import HttpResponse, RateLimitError, RetryPolicy, read_json_with_retries


class _RateLimitedTransport:
    def __init__(self, retry_after: str) -> None:
        self.retry_after = retry_after
        self.calls = 0

    def request(self, method, url, *, headers, body, timeout):
        self.calls += 1
        return HttpResponse(status=429, headers={"Retry-After": self.retry_after})


class ProviderRetryAfterLivenessTests(unittest.TestCase):
    def test_excessive_retry_after_fails_closed_without_sleeping(self) -> None:
        transport = _RateLimitedTransport("600")
        sleeps = []
        with self.assertRaises(RateLimitError) as caught:
            read_json_with_retries(
                transport,
                "GET",
                "https://provider.invalid/read",
                headers={},
                timeout=15.0,
                retry_policy=RetryPolicy(max_attempts=3, max_retry_after_seconds=30.0),
                sleeper=sleeps.append,
                operation="test.read",
            )
        self.assertEqual(caught.exception.retry_after, 600.0)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(sleeps, [])

    def test_bounded_retry_after_is_respected(self) -> None:
        transport = _RateLimitedTransport("3")
        sleeps = []
        with self.assertRaises(RateLimitError):
            read_json_with_retries(
                transport,
                "GET",
                "https://provider.invalid/read",
                headers={},
                timeout=15.0,
                retry_policy=RetryPolicy(max_attempts=2, max_retry_after_seconds=30.0),
                sleeper=sleeps.append,
                operation="test.read",
            )
        self.assertEqual(transport.calls, 2)
        self.assertEqual(sleeps, [3.0])


if __name__ == "__main__":
    unittest.main()
