from __future__ import annotations

import argparse
import json
import os
from typing import Any, Mapping

from . import initial_lineage_runtime as runtime

SCHEMA_VERSION = "1.0"
ATTEMPTS_ENV = "UES_INITIAL_LINEAGE_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS"
DEFAULT_ATTEMPTS = 2
MAX_ATTEMPTS = 3


def _attempt_limit(raw: str | None = None) -> int:
    value = str(raw if raw is not None else os.environ.get(ATTEMPTS_ENV, "")).strip()
    if not value:
        return DEFAULT_ATTEMPTS
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_ATTEMPTS
    return max(1, min(parsed, MAX_ATTEMPTS))


def _retryable_zero_effect_provider_read(result: Mapping[str, Any]) -> bool:
    return (
        str(result.get("result") or "") == runtime._PROVIDER_READ_UNAVAILABLE_RESULT
        and result.get("provider_write_attempted") is False
        and int(result.get("external_effects_dispatched") or 0) == 0
        and int(result.get("new_tasks_or_sessions_created") or 0) == 0
        and result.get("safe_to_blind_retry") is False
    )


def run(project: str) -> dict[str, Any]:
    limit = _attempt_limit()
    for attempt in range(1, limit + 1):
        result = dict(runtime.run(project))
        result["provider_snapshot_attempts"] = attempt
        result["provider_snapshot_retries_used"] = attempt - 1
        result["provider_snapshot_attempt_limit"] = limit
        result["provider_snapshot_retry_mode"] = "BOUNDED_ZERO_EFFECT_PRE_EFFECT_READ_ONLY"
        if not _retryable_zero_effect_provider_read(result) or attempt >= limit:
            return result
    raise AssertionError("bounded initial-lineage retry loop exhausted without a result")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run initial-lineage runtime with bounded pre-effect provider-read recovery")
    parser.add_argument("project", choices=sorted(runtime.SUPPORTED_PROJECTS))
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if str(result.get("result") or "") == runtime._PROVIDER_READ_UNAVAILABLE_RESULT:
        return runtime._PROVIDER_READ_UNAVAILABLE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
