from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import terminal_recovery as recovery
from . import terminal_results
from .state_store import StateUnavailable


def run_read_only_backfill(
    project_names: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run terminal backfill with a pre-provider-read lineage cache for materialization.

    The cache is read-only identity evidence captured from StateStore before Jules
    content reads. It is used only if StateStore discovery becomes unavailable after
    provider content was already read. Persistence and authoritative readback still
    use the live StateStore and therefore remain fail-closed; cached state can never
    make a CAS appear successful.
    """

    canonical_index = terminal_results.lineage_index
    original_results_index = terminal_results.lineage_index
    original_recovery_index = recovery.lineage_index
    cache: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}

    def cached_index(live_store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
        key = (str(project), str(route))
        try:
            value = canonical_index(live_store, project=project, route=route)
        except StateUnavailable:
            if key in cache:
                return cache[key]
            raise
        # Never replace a previously proven non-empty exact identity cache with an
        # empty view produced during a later partial read outage. An empty first
        # read remains authoritative and cannot be upgraded by inference.
        if value or key not in cache:
            cache[key] = {
                str(fp): [dict(item) for item in matches]
                for fp, matches in value.items()
            }
        return value

    terminal_results.lineage_index = cached_index
    recovery.lineage_index = cached_index
    try:
        return recovery.run_read_only_backfill(
            project_names,
            store=store,
            client=client,
        )
    finally:
        terminal_results.lineage_index = original_results_index
        recovery.lineage_index = original_recovery_index
