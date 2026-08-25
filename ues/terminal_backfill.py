from __future__ import annotations

import argparse
import json
from typing import Any

from .state_backends.recovery_same_repo import build_recovery_state_store
from .terminal_recovery import run_read_only_backfill


def _category(exc: BaseException) -> str:
    text = str(exc)
    if "HTTP 403" in text:
        return "GITHUB_REF_HTTP_403_UNAVAILABLE"
    if "HTTP 429" in text:
        return "GITHUB_REF_HTTP_429_UNAVAILABLE"
    if "unavailable" in text.lower():
        return "GITHUB_REF_NETWORK_OR_TRANSPORT_UNAVAILABLE"
    return str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120]


def run(projects: list[str] | None = None) -> dict[str, Any]:
    try:
        store = build_recovery_state_store()
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
    return run_read_only_backfill(projects, store=store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES generic terminal-result backfill")
    parser.add_argument("projects", nargs="*", help="optional governed adapter project IDs")
    args = parser.parse_args(argv)
    result = run(args.projects or None)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
