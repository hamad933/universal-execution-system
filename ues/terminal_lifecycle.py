from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any, Mapping

from . import observation_backed_health as health
from .terminal_recovery import load_governed_projects


_UNSTRUCTURED_RESULT_STATE = "COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION"


def _adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().upper()
    matches = [item for item in load_governed_projects([name]) if item["project"] == name]
    if len(matches) != 1:
        raise ValueError("exact governed adapter identity required")
    return dict(matches[0])


def _normalize_unstructured_reviewer_views(result: Mapping[str, Any]) -> dict[str, Any]:
    """Do not turn a missing structured handoff into a false reviewed-SHA claim.

    Durable legacy terminal results can predate a reviewed_sha field. The generic
    current-view freshness layer correctly rejects a structured Reviewer result
    whose reviewed_sha differs from the current candidate, but a result that has no
    verdict, no finding count and no reviewed_sha is still unstructured evidence.
    Preserve that fail-closed state instead of reporting REVIEWED_SHA_MISMATCH.
    """

    output = dict(result)
    normalized_results: list[dict[str, Any]] = []
    for raw in output.get("results") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if (
            str(item.get("role") or "").upper() in {"REVIEWER", "ASSURANCE"}
            and item.get("result_state") == "REVIEWED_SHA_MISMATCH"
            and not item.get("reviewed_sha")
            and item.get("verdict") is None
            and item.get("finding_count") is None
        ):
            item["result_state"] = _UNSTRUCTURED_RESULT_STATE
            item["freshness_status"] = "UNADJUDICABLE"
            item["parent_action_required"] = True
            item["safe_read_only_recovery_exists"] = True
        normalized_results.append(item)
    output["results"] = normalized_results

    summary = dict(output.get("summary") or {})
    if normalized_results:
        counts = Counter(str(item.get("result_state") or "UNKNOWN") for item in normalized_results)
        summary["binding_counts"] = dict(sorted(counts.items()))
        summary["parent_consumable_result_count"] = sum(
            item.get("result_state") == "PARENT_CONSUMABLE" for item in normalized_results
        )
        summary["terminal_result_count"] = len(normalized_results)
        summary["terminal_unconsumed_result_count"] = (
            len(normalized_results) - int(summary["parent_consumable_result_count"])
        )
    output["summary"] = summary
    return output


def run(project: str) -> dict[str, Any]:
    # observation_backed_health already builds the canonical live StateStore.
    # This wrapper grants no project/provider authority and adds no provider call.
    result = _normalize_unstructured_reviewer_views(
        health.run_observation_backed_no_effect_health(
            _adapter(project),
            authority=None,
        )
    )
    result["terminal_recovery_lifecycle"] = True
    result["runtime_wrapper_grants_authority"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES durable terminal-result lifecycle readback")
    parser.add_argument("project")
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
