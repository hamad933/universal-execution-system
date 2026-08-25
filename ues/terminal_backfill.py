from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .live_runtime import build_live_state_store
from .providers.base import RetryPolicy
from .providers.jules import JulesClient
from .terminal_recovery_runtime import run_read_only_backfill

_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
_MAX_PROVIDER_TIMEOUT_SECONDS = 10.0
_DEFAULT_PROVIDER_READ_ATTEMPTS = 2
_MAX_PROVIDER_READ_ATTEMPTS = 2


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

    Terminal-result recovery already has a workflow-level deadline.  Using the
    general-purpose Jules 15-second / three-attempt read policy inside a bounded
    worker pool can still let slow pages consume that entire deadline.  Backfill
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
    result = run_read_only_backfill(projects, store=store, client=_backfill_provider_client())
    result["terminal_backfill_provider_timeout_seconds"] = timeout
    result["terminal_backfill_provider_read_attempts"] = attempts
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
