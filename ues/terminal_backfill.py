from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from .live_runtime import build_live_state_store
from .providers.base import NetworkError, RateLimitError, RetryPolicy, ServerError
from .providers.jules import JulesClient
from .terminal_recovery_runtime import run_read_only_backfill

_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
_MAX_PROVIDER_TIMEOUT_SECONDS = 10.0
_DEFAULT_PROVIDER_READ_ATTEMPTS = 2
_MAX_PROVIDER_READ_ATTEMPTS = 2
_DEFAULT_INVENTORY_SNAPSHOT_ATTEMPTS = 2
_MAX_INVENTORY_SNAPSHOT_ATTEMPTS = 3
_TRANSIENT_INVENTORY_ERRORS = (NetworkError, RateLimitError, ServerError)


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _backfill_provider_policy() -> tuple[float, int]:
    """Return a bounded GET-only provider policy for terminal backfill.

    Terminal-result recovery already has a workflow-level deadline. Using the
    general-purpose Jules 15-second / three-attempt read policy inside a bounded
    worker pool can still let slow pages consume that entire deadline. Backfill
    therefore uses a deliberately shorter read budget while retaining bounded
    retries and the canonical per-session failure classification.
    """

    timeout = _bounded_float_env(
        "UES_TERMINAL_BACKFILL_PROVIDER_TIMEOUT_SECONDS",
        _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        minimum=1.0,
        maximum=_MAX_PROVIDER_TIMEOUT_SECONDS,
    )
    attempts = _bounded_int_env(
        "UES_TERMINAL_BACKFILL_PROVIDER_READ_ATTEMPTS",
        _DEFAULT_PROVIDER_READ_ATTEMPTS,
        minimum=1,
        maximum=_MAX_PROVIDER_READ_ATTEMPTS,
    )
    return timeout, attempts


def _inventory_snapshot_attempts() -> int:
    """Bound full GET-only inventory snapshot retries independently of HTTP retries."""

    return _bounded_int_env(
        "UES_TERMINAL_BACKFILL_INVENTORY_SNAPSHOT_ATTEMPTS",
        _DEFAULT_INVENTORY_SNAPSHOT_ATTEMPTS,
        minimum=1,
        maximum=_MAX_INVENTORY_SNAPSHOT_ATTEMPTS,
    )


class _InventorySnapshotRetryClient:
    """Retry only transient full provider inventory snapshots for terminal backfill.

    JulesClient already retries individual HTTP requests. A paginated inventory can
    still fail after those request-level retries are exhausted. Repeating that
    complete GET-only inventory read is safe and bounded; it never retries an
    Activity read or any provider mutation. Sources and sessions are independent so
    a successful source snapshot is not repeated because session enumeration failed.
    """

    def __init__(self, delegate: JulesClient, *, attempts: int) -> None:
        self._delegate = delegate
        self._attempts = max(1, min(_MAX_INVENTORY_SNAPSHOT_ATTEMPTS, int(attempts)))
        self.source_inventory_attempts = 0
        self.session_inventory_attempts = 0

    def _read_inventory(self, read: Callable[[], list[dict[str, Any]]], *, kind: str) -> list[dict[str, Any]]:
        for attempt in range(1, self._attempts + 1):
            if kind == "sources":
                self.source_inventory_attempts = attempt
            else:
                self.session_inventory_attempts = attempt
            try:
                return list(read())
            except _TRANSIENT_INVENTORY_ERRORS:
                if attempt >= self._attempts:
                    raise
        raise RuntimeError("unreachable terminal-backfill inventory retry state")

    def list_sources(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._read_inventory(
            lambda: self._delegate.list_sources(page_size=page_size),
            kind="sources",
        )

    def list_sessions(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._read_inventory(
            lambda: self._delegate.list_sessions(page_size=page_size),
            kind="sessions",
        )

    def list_activities(self, session: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._delegate.list_activities(session, page_size=page_size)


def _backfill_provider_client() -> JulesClient | None:
    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    if not key:
        return None
    timeout, attempts = _backfill_provider_policy()
    return JulesClient(
        key,
        timeout=timeout,
        read_retry_policy=RetryPolicy(
            max_attempts=attempts,
            base_delay_seconds=0.25,
            max_delay_seconds=1.0,
            max_retry_after_seconds=5.0,
        ),
    )


def _category(exc: BaseException) -> str:
    text = str(exc)
    if "HTTP 403" in text:
        return "GITHUB_REF_HTTP_403_UNAVAILABLE"
    if "HTTP 429" in text:
        return "GITHUB_REF_HTTP_429_UNAVAILABLE"
    if "transport" in text.lower() or "unavailable" in text.lower():
        return "GITHUB_REF_NETWORK_OR_TRANSPORT_UNAVAILABLE"
    return str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120]


def run(projects: list[str] | None = None) -> dict[str, Any]:
    try:
        store = build_live_state_store()
    except Exception as exc:
        return {
            "schema_version": "1.0",
            "result": "TERMINAL_BACKFILL_STATESTORE_UNAVAILABLE_BEFORE_PROVIDER_READ",
            "error_category": _category(exc),
            "provider_read_started": False,
            "provider_read_complete": False,
            "state_persistence_complete": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
        }
    timeout, attempts = _backfill_provider_policy()
    inventory_attempt_limit = _inventory_snapshot_attempts()
    provider = _backfill_provider_client()
    retrying_provider = (
        _InventorySnapshotRetryClient(provider, attempts=inventory_attempt_limit)
        if provider is not None
        else None
    )
    result = run_read_only_backfill(projects, store=store, client=retrying_provider)
    result["terminal_backfill_provider_timeout_seconds"] = timeout
    result["terminal_backfill_provider_read_attempts"] = attempts
    result["terminal_backfill_inventory_snapshot_attempt_limit"] = inventory_attempt_limit
    result["terminal_backfill_source_inventory_attempts"] = (
        retrying_provider.source_inventory_attempts if retrying_provider is not None else 0
    )
    result["terminal_backfill_session_inventory_attempts"] = (
        retrying_provider.session_inventory_attempts if retrying_provider is not None else 0
    )
    result["terminal_backfill_inventory_snapshot_retry_get_only"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES generic terminal-result backfill")
    parser.add_argument("projects", nargs="*", help="optional governed adapter project IDs")
    args = parser.parse_args(argv)
    result = run(args.projects or None)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
