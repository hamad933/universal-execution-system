import pytest

from ues.providers.base import HttpResponse, RateLimitError, RetryPolicy, read_json_with_retries


class _RateLimitedTransport:
    def __init__(self, retry_after: str) -> None:
        self.retry_after = retry_after
        self.calls = 0

    def request(self, method, url, *, headers, body, timeout):
        self.calls += 1
        return HttpResponse(status=429, headers={"Retry-After": self.retry_after})


def test_excessive_retry_after_fails_closed_without_sleeping() -> None:
    transport = _RateLimitedTransport("600")
    sleeps = []
    with pytest.raises(RateLimitError) as exc_info:
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
    assert exc_info.value.retry_after == 600.0
    assert transport.calls == 1
    assert sleeps == []


def test_bounded_retry_after_is_respected() -> None:
    transport = _RateLimitedTransport("3")
    sleeps = []
    with pytest.raises(RateLimitError):
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
    assert transport.calls == 2
    assert sleeps == [3.0]
