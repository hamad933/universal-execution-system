from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class ProviderError(RuntimeError):
    category = "PROVIDER_ERROR"

    def __init__(
        self,
        message: str = "provider request failed",
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.operation = operation

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status_code": self.status_code,
            "retry_after": self.retry_after,
            "operation": self.operation,
        }


class AuthenticationError(ProviderError):
    category = "AUTHENTICATION_ERROR"


class AuthorizationError(ProviderError):
    category = "AUTHORIZATION_ERROR"


class NotFoundError(ProviderError):
    category = "NOT_FOUND"


class RateLimitError(ProviderError):
    category = "RATE_LIMITED"


class ServerError(ProviderError):
    category = "SERVER_ERROR"


class NetworkError(ProviderError):
    category = "NETWORK_ERROR"


class ProtocolError(ProviderError):
    category = "PROTOCOL_ERROR"


class SessionContinuationUnavailable(ProviderError):
    category = "SESSION_CONTINUATION_UNAVAILABLE"


class WriteOutcomeUnknown(ProviderError):
    category = "WRITE_OUTCOME_UNKNOWN"

    def __init__(self, *args: Any, recovery: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recovery = dict(recovery or {})

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["recovery"] = self.recovery
        return result


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ValueError("retry delays must be non-negative")


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read() if exc.fp else b"",
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise NetworkError("provider network request failed") from exc


def retry_after_seconds(value: str | None, *, now: Callable[[], float] = time.time) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            return max(0.0, retry_at.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


def error_for_response(response: HttpResponse, *, operation: str) -> ProviderError:
    status = response.status
    retry_after = retry_after_seconds(_header(response.headers, "Retry-After"))
    if status == 401:
        return AuthenticationError("provider authentication failed", status_code=status, operation=operation)
    if status == 403:
        return AuthorizationError("provider authorization failed", status_code=status, operation=operation)
    if status == 404:
        return NotFoundError("provider resource not found", status_code=status, operation=operation)
    if status == 429:
        return RateLimitError(
            "provider rate limit reached",
            status_code=status,
            retry_after=retry_after,
            operation=operation,
        )
    if 500 <= status <= 599:
        return ServerError("provider server error", status_code=status, operation=operation)
    return ProtocolError("unexpected provider HTTP status", status_code=status, operation=operation)


def read_json_with_retries(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    retry_policy: RetryPolicy,
    sleeper: Callable[[float], None],
    operation: str,
) -> Any:
    last_error: ProviderError | None = None
    for attempt in range(1, retry_policy.max_attempts + 1):
        try:
            response = transport.request(method, url, headers=headers, body=None, timeout=timeout)
        except NetworkError:
            last_error = NetworkError("provider network request failed", operation=operation)
        else:
            if 200 <= response.status <= 299:
                if not response.body:
                    return {}
                try:
                    return json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("provider returned invalid JSON", operation=operation) from exc
            error = error_for_response(response, operation=operation)
            if not isinstance(error, (RateLimitError, ServerError)):
                raise error
            last_error = error

        if attempt >= retry_policy.max_attempts:
            assert last_error is not None
            raise last_error

        delay = min(
            retry_policy.max_delay_seconds,
            retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        if isinstance(last_error, RateLimitError) and last_error.retry_after is not None:
            if last_error.retry_after > retry_policy.max_retry_after_seconds:
                raise last_error
            delay = last_error.retry_after
        sleeper(delay)

    raise AssertionError("unreachable")


def encode_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None
