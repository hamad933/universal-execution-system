from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue
import time
from typing import Any, Callable

from .live_runtime import build_live_state_store
from .providers.base import NetworkError, RateLimitError, RetryPolicy, ServerError
from .providers.jules import JulesClient
from .terminal_recovery_runtime import run_read_only_backfill

_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
_MAX_PROVIDER_TIMEOUT_SECONDS = 10.0
_DEFAULT_PROVIDER_READ_ATTEMPTS = 2
_MAX_PROVIDER_READ_ATTEMPTS = 2
_DEFAULT_INVENTORY_PROVIDER_TIMEOUT_SECONDS = 15.0
_MAX_INVENTORY_PROVIDER_TIMEOUT_SECONDS = 30.0
_DEFAULT_INVENTORY_PROVIDER_READ_ATTEMPTS = 3
_MAX_INVENTORY_PROVIDER_READ_ATTEMPTS = 3
_DEFAULT_INVENTORY_SNAPSHOT_ATTEMPTS = 2
_MAX_INVENTORY_SNAPSHOT_ATTEMPTS = 3
_DEFAULT_WALL_CLOCK_BUDGET_SECONDS = 40 * 60
_MIN_WALL_CLOCK_BUDGET_SECONDS = 30 * 60
_MAX_WALL_CLOCK_BUDGET_SECONDS = 40 * 60
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


def _backfill_wall_clock_budget_seconds() -> int:
    return int(
        _bounded_float_env(
            "UES_TERMINAL_BACKFILL_WALL_CLOCK_BUDGET_SECONDS",
            float(_DEFAULT_WALL_CLOCK_BUDGET_SECONDS),
            minimum=float(_MIN_WALL_CLOCK_BUDGET_SECONDS),
            maximum=float(_MAX_WALL_CLOCK_BUDGET_SECONDS),
        )
    )


def _budget_exhausted_result(
    *,
    phase: str,
    completed_phases: list[str],
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "result": "TERMINAL_BACKFILL_BUDGET_EXHAUSTED",
        "budget_exhausted_phase": phase,
        "completed_phases": list(completed_phases),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "provider_read_started": "provider_clients_ready" in completed_phases,
        "provider_read_complete": False,
        "state_persistence_complete": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "provider_mutation_performed": False,
        "safe_to_blind_retry": False,
    }


def _backfill_provider_policy() -> tuple[float, int]:
    """Return the bounded GET-only Activity policy for terminal backfill.

    Activity retrieval fans out in a bounded worker pool, so it deliberately keeps
    a short per-request budget. Provider inventory is read with a separate policy
    because a complete paginated sources/sessions snapshot is a single prerequisite
    for the whole backfill and must not inherit the Activity worker timeout.
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


def _inventory_provider_policy() -> tuple[float, int]:
    """Return a bounded provider policy dedicated to full inventory snapshots."""

    timeout = _bounded_float_env(
        "UES_TERMINAL_BACKFILL_INVENTORY_PROVIDER_TIMEOUT_SECONDS",
        _DEFAULT_INVENTORY_PROVIDER_TIMEOUT_SECONDS,
        minimum=5.0,
        maximum=_MAX_INVENTORY_PROVIDER_TIMEOUT_SECONDS,
    )
    attempts = _bounded_int_env(
        "UES_TERMINAL_BACKFILL_INVENTORY_PROVIDER_READ_ATTEMPTS",
        _DEFAULT_INVENTORY_PROVIDER_READ_ATTEMPTS,
        minimum=1,
        maximum=_MAX_INVENTORY_PROVIDER_READ_ATTEMPTS,
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
    """Retry transient full inventory snapshots while keeping Activities short-bound.

    Inventory and Activity reads intentionally use separate Jules clients. A complete
    paginated inventory may need a longer request budget than a single Activity page;
    sharing the short Activity timeout made all-project backfill fragile. Outer retry
    remains inventory-only and GET-only. Activity reads are delegated once to the
    short-budget client and are never outer-retried here.
    """

    def __init__(
        self,
        delegate: JulesClient,
        *,
        attempts: int,
        activity_delegate: JulesClient | None = None,
    ) -> None:
        self._delegate = delegate
        self._activity_delegate = activity_delegate or delegate
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
        return self._activity_delegate.list_activities(session, page_size=page_size)


def _provider_client(timeout: float, attempts: int) -> JulesClient | None:
    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    if not key:
        return None
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


def _backfill_provider_client() -> JulesClient | None:
    timeout, attempts = _backfill_provider_policy()
    return _provider_client(timeout, attempts)


def _inventory_provider_client() -> JulesClient | None:
    timeout, attempts = _inventory_provider_policy()
    return _provider_client(timeout, attempts)


def _category(exc: BaseException) -> str:
    text = str(exc)
    if "HTTP 403" in text:
        return "GITHUB_REF_HTTP_403_UNAVAILABLE"
    if "HTTP 429" in text:
        return "GITHUB_REF_HTTP_429_UNAVAILABLE"
    if "transport" in text.lower() or "unavailable" in text.lower():
        return "GITHUB_REF_NETWORK_OR_TRANSPORT_UNAVAILABLE"
    return str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120]


def _emit_phase(callback: Callable[[str], None] | None, phase: str) -> None:
    if callback is not None:
        callback(phase)


def run(
    projects: list[str] | None = None,
    *,
    phase_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
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
    _emit_phase(phase_callback, "state_store")

    timeout, attempts = _backfill_provider_policy()
    inventory_timeout, inventory_read_attempts = _inventory_provider_policy()
    inventory_attempt_limit = _inventory_snapshot_attempts()
    activity_provider = _backfill_provider_client()
    inventory_provider = _inventory_provider_client()
    retrying_provider = (
        _InventorySnapshotRetryClient(
            inventory_provider,
            attempts=inventory_attempt_limit,
            activity_delegate=activity_provider,
        )
        if inventory_provider is not None
        else None
    )
    _emit_phase(phase_callback, "provider_clients_ready")
    result = run_read_only_backfill(projects, store=store, client=retrying_provider)
    _emit_phase(phase_callback, "read_only_recovery")
    result["terminal_backfill_provider_timeout_seconds"] = timeout
    result["terminal_backfill_provider_read_attempts"] = attempts
    result["terminal_backfill_inventory_provider_timeout_seconds"] = inventory_timeout
    result["terminal_backfill_inventory_provider_read_attempts"] = inventory_read_attempts
    result["terminal_backfill_inventory_snapshot_attempt_limit"] = inventory_attempt_limit
    result["terminal_backfill_source_inventory_attempts"] = (
        retrying_provider.source_inventory_attempts if retrying_provider is not None else 0
    )
    result["terminal_backfill_session_inventory_attempts"] = (
        retrying_provider.session_inventory_attempts if retrying_provider is not None else 0
    )
    result["terminal_backfill_inventory_snapshot_retry_get_only"] = True
    result["terminal_backfill_split_inventory_activity_read_policy"] = True
    return result


def _run_in_worker(
    projects: list[str] | None,
    result_queue: Any,
    phase_queue: Any,
) -> None:
    def record_phase(phase: str) -> None:
        phase_queue.put(phase)

    try:
        result_queue.put(run(projects, phase_callback=record_phase))
    except BaseException as exc:
        result_queue.put(
            {
                "schema_version": "1.0",
                "result": "TERMINAL_BACKFILL_WORKER_FAILED",
                "error_category": _category(exc),
                "provider_read_complete": False,
                "state_persistence_complete": False,
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
                "provider_mutation_performed": False,
                "safe_to_blind_retry": False,
            }
        )


def _drain_completed_phases(phase_queue: Any) -> list[str]:
    completed: list[str] = []
    while True:
        try:
            phase = phase_queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(phase, str) and phase and phase not in completed:
            completed.append(phase)
    return completed


def _run_bounded(projects: list[str] | None) -> dict[str, Any]:
    budget = _backfill_wall_clock_budget_seconds()
    started = time.monotonic()
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    phase_queue = context.Queue()
    worker = context.Process(
        target=_run_in_worker,
        args=(projects, result_queue, phase_queue),
        daemon=False,
    )
    worker.start()
    worker.join(timeout=budget)

    if worker.is_alive():
        completed_phases = _drain_completed_phases(phase_queue)
        worker.terminate()
        worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        current_phase = "read_only_recovery"
        if "provider_clients_ready" not in completed_phases:
            current_phase = "state_store"
        elif "read_only_recovery" in completed_phases:
            current_phase = "post_recovery_finalize"
        return _budget_exhausted_result(
            phase=current_phase,
            completed_phases=completed_phases,
            elapsed_seconds=time.monotonic() - started,
        )

    try:
        result = result_queue.get(timeout=5)
    except queue.Empty:
        return {
            "schema_version": "1.0",
            "result": "TERMINAL_BACKFILL_WORKER_EXITED_WITHOUT_RESULT",
            "worker_exit_code": worker.exitcode,
            "provider_read_complete": False,
            "state_persistence_complete": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
        }
    if not isinstance(result, dict):
        return {
            "schema_version": "1.0",
            "result": "TERMINAL_BACKFILL_WORKER_RETURN_INVALID",
            "provider_read_complete": False,
            "state_persistence_complete": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
        }
    result["terminal_backfill_wall_clock_budget_seconds"] = budget
    result["terminal_backfill_wall_clock_guard_process_isolated"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES generic terminal-result backfill")
    parser.add_argument("projects", nargs="*", help="optional governed adapter project IDs")
    args = parser.parse_args(argv)
    result = _run_bounded(args.projects or None)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
