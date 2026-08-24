from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Callable, Mapping

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_current as current
from .identity import canonical_lane_id
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "1.0"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _bounded_env_text(env: Mapping[str, str], key: str, *, limit: int = 512) -> str | None:
    value = str(env.get(key) or "").strip()
    if not value or len(value) > limit:
        return None
    return value


def runtime_binding_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return sanitized execution telemetry; it never grants mutation authority."""

    source = os.environ if env is None else env
    repository = _bounded_env_text(source, "GITHUB_REPOSITORY", limit=200)
    sha = _bounded_env_text(source, "GITHUB_SHA", limit=40)
    github_actions = str(source.get("GITHUB_ACTIONS") or "").strip().lower() == "true"

    if not github_actions or repository is None or sha is None or not _REPOSITORY.fullmatch(repository) or not _SHA.fullmatch(sha):
        return {
            "status": "UNBOUND",
            "source": "GITHUB_ACTIONS_RUNTIME_ENV",
            "telemetry_grants_no_authority": True,
        }

    result: dict[str, Any] = {
        "status": "BOUND",
        "repository": repository,
        "sha": sha.lower(),
        "source": "GITHUB_ACTIONS_RUNTIME_ENV",
        "telemetry_grants_no_authority": True,
    }
    for env_key, output_key, limit in (
        ("GITHUB_REF", "ref", 512),
        ("GITHUB_REF_NAME", "ref_name", 256),
        ("GITHUB_EVENT_NAME", "event_name", 128),
        ("GITHUB_WORKFLOW_REF", "workflow_ref", 512),
    ):
        value = _bounded_env_text(source, env_key, limit=limit)
        if value is not None:
            result[output_key] = value

    for env_key, output_key in (("GITHUB_RUN_ID", "run_id"), ("GITHUB_RUN_ATTEMPT", "run_attempt")):
        value = _bounded_env_text(source, env_key, limit=32)
        if value is not None and value.isdigit():
            result[output_key] = int(value)
    return result


def _persist_health_with_runtime_binding(
    original: Callable[..., dict[str, Any]],
    runtime_binding: Mapping[str, Any],
) -> Callable[..., dict[str, Any]]:
    """Decorate health writes with the exact sanitized runtime execution binding."""

    binding = dict(runtime_binding)

    def persist(
        store: Any,
        *,
        project: str,
        route: str,
        status: str,
        summary: Mapping[str, Any],
        error_category: str | None = None,
    ) -> dict[str, Any]:
        original_result = original(
            store,
            project=project,
            route=route,
            status=status,
            summary=summary,
            error_category=error_category,
        )
        lane_id = canonical_lane_id(project, route, legacy.HEALTH_WORKSTREAM)
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lifecycle health unavailable for runtime binding")

        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        record.last_observed_github_state = dict(binding)
        provenance = dict(record.authority_provenance or {})
        provenance["runtime_binding_grants_no_authority"] = True
        record.authority_provenance = provenance
        saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        if saved.status != "OK":
            raise StateUnavailable(saved.reason or "failed to persist lifecycle runtime binding")

        result = dict(original_result)
        result["version"] = saved.version
        result["runtime_binding_status"] = str(binding.get("status") or "UNBOUND")
        if binding.get("status") == "BOUND":
            result["runtime_sha"] = binding.get("sha")
        return result

    return persist


def _promote_effect_counts(result: dict[str, Any]) -> dict[str, Any]:
    """Expose bounded effect counters at the runtime envelope boundary.

    The counters already exist inside V2 summary. Promoting them makes durable
    control-plane receipts able to prove zero/nonzero effects without parsing an
    implementation-specific nested payload. Missing counters remain missing rather
    than being guessed.
    """

    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        return result
    for key in ("external_effects_dispatched", "new_tasks_or_sessions_created"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and key not in result:
            result[key] = value
    return result


def run(project: str) -> dict[str, Any]:
    runtime_binding = runtime_binding_from_env()
    original = legacy._persist_health
    legacy._persist_health = _persist_health_with_runtime_binding(original, runtime_binding)
    try:
        result = dict(current.run(project))
    finally:
        legacy._persist_health = original
    _promote_effect_counts(result)
    result["observed_runtime_binding"] = runtime_binding
    result["runtime_binding_grants_authority"] = False
    result["observability_schema_version"] = SCHEMA_VERSION
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES current-authority lifecycle with exact runtime receipt binding")
    parser.add_argument("project", choices=["CEP", "GS", "cep", "gs"])
    args = parser.parse_args(argv)
    print(json.dumps(run(args.project), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
