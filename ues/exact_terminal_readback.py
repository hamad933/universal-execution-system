from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping

from . import terminal_backfill
from . import terminal_recovery as recovery
from . import terminal_results

_ALLOWED_PROJECTS = frozenset({"RP01", "RP02", "RP03", "RP04"})
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _filtered_index(indexer: Any, target_workstream: str):
    def wrapped(store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
        index = indexer(store, project=project, route=route)
        result: dict[str, list[dict[str, Any]]] = {}
        for fingerprint, matches in index.items():
            selected = [dict(item) for item in matches if str(item.get("workstream") or "") == target_workstream]
            if selected:
                result[str(fingerprint)] = selected
        return result
    return wrapped


def run(project: str, workstream: str) -> dict[str, Any]:
    project_id = str(project or "").strip().upper()
    target = str(workstream or "").strip()
    if project_id not in _ALLOWED_PROJECTS:
        raise ValueError("exact terminal readback project must be RP01-RP04")
    if not _WORKSTREAM.fullmatch(target):
        raise ValueError("exact terminal readback workstream is invalid")

    original_results_index = terminal_results.lineage_index
    original_recovery_index = recovery.lineage_index
    original_pending = recovery._pending_identity_candidates

    # Exact-workstream readback is deliberately for a known durable lineage.
    # It must not reconcile unrelated/pending identities while consuming one
    # completed result. Provider inventory remains GET-only and authoritative.
    terminal_results.lineage_index = _filtered_index(original_results_index, target)
    recovery.lineage_index = _filtered_index(original_recovery_index, target)
    recovery._pending_identity_candidates = lambda store, projects: {}
    try:
        result = terminal_backfill.run([project_id])
    finally:
        terminal_results.lineage_index = original_results_index
        recovery.lineage_index = original_recovery_index
        recovery._pending_identity_candidates = original_pending

    result["exact_workstream_filter"] = target
    result["exact_workstream_readback"] = True
    result["pending_identity_reconciliation_performed"] = False
    result["provider_mutation_performed"] = False
    result["new_tasks_or_sessions_created"] = 0
    result["safe_to_blind_retry"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES exact-workstream terminal-result readback")
    parser.add_argument("project", choices=sorted(_ALLOWED_PROJECTS))
    parser.add_argument("workstream")
    args = parser.parse_args(argv)
    result = run(args.project, args.workstream)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
