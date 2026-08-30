from __future__ import annotations

import os as _bootstrap_os
from pathlib import Path as _BootstrapPath
from typing import Any as _BootstrapAny, Callable as _BootstrapCallable

# Preserve the existing initial-lineage runtime verbatim while inserting the
# bounded pre-effect inventory retry at this module boundary. Executing the
# preserved source in this module's globals keeps existing patch/import
# semantics intact for callers and tests.
_bootstrap_name = globals().get("__name__", "ues.initial_lineage_runtime")
_core_path = _BootstrapPath(__file__).with_name("_initial_lineage_runtime_core.py")
globals()["__name__"] = "ues.initial_lineage_runtime"
exec(compile(_core_path.read_text(encoding="utf-8"), str(_core_path), "exec"), globals(), globals())
globals()["__name__"] = _bootstrap_name

_DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 2
_MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 3
_PROVIDER_INVENTORY_ATTEMPTS_ENV = "UES_INITIAL_LINEAGE_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS"


def _provider_inventory_snapshot_attempts() -> int:
    raw = str(_bootstrap_os.environ.get(_PROVIDER_INVENTORY_ATTEMPTS_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    try:
        configured = int(raw)
    except ValueError:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    return max(1, min(configured, _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS))


def _provider_inventory_with_retry(
    client: object,
    attempt_limit: int | None = None,
    *,
    provider_inventory: _BootstrapCallable[[object], list[dict[str, _BootstrapAny]]] | None = None,
) -> tuple[list[dict[str, _BootstrapAny]], int, int]:
    inventory_reader = provider_inventory or legacy._provider_inventory
    limit = _provider_inventory_snapshot_attempts() if attempt_limit is None else int(attempt_limit)
    limit = max(1, min(limit, _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS))
    attempts = 0
    while True:
        attempts += 1
        try:
            inventory = inventory_reader(client)
            return inventory, attempts, limit
        except _PRE_EFFECT_PROVIDER_READ_ERRORS as exc:
            if not _is_pre_effect_provider_read_failure(exc) or attempts >= limit:
                raise


_initial_lineage_run_without_inventory_retry = run


def run(project: str) -> dict[str, _BootstrapAny]:
    original_inventory_reader = legacy._provider_inventory

    def bounded_inventory_reader(client: object) -> list[dict[str, _BootstrapAny]]:
        inventory, _, _ = _provider_inventory_with_retry(
            client,
            provider_inventory=original_inventory_reader,
        )
        return inventory

    legacy._provider_inventory = bounded_inventory_reader
    try:
        return _initial_lineage_run_without_inventory_retry(project)
    finally:
        legacy._provider_inventory = original_inventory_reader


if _bootstrap_name == "__main__":
    raise SystemExit(main())
