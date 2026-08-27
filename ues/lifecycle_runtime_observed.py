from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from typing import Any, Callable, Mapping

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_current as current
from .identity import canonical_lane_id
from .providers.base import NetworkError, RateLimitError, ServerError
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord

SCHEMA_VERSION = "1.0"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_PRE_EFFECT_PROVIDER_READ_OPERATIONS = frozenset({"jules.sessions.list", "jules.sessions.get"})
_PRE_EFFECT_PROVIDER_READ_ERRORS = (NetworkError, RateLimitError, ServerError)
_PROVIDER_READ_UNAVAILABLE_RESULT = "PROVIDER_READ_UNAVAILABLE_BEFORE_EFFECTS"
_PROVIDER_READ_UNAVAILABLE_EXIT = 75
_TELEMETRY_WRITE_ERRORS = (StateUnavailable, StateVersionConflict)


def _bounded_env_text(env: Mapping[str, str], key: str, *, limit: int = 512) -> str | None:
    value = str(env.get(key) or "").strip()
    if not value or len(value) > limit:
        return None
    return value


def _checked_out_runtime_sha(repository: str) -> str | None:
    """Return Git HEAD only when the checkout origin matches the runtime repository."""

    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip().lower()
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    normalized_remote = remote.rstrip("/")
    if normalized_remote.endswith(".git"):
        normalized_remote = normalized_remote[:-4]
    repository_lower = repository.lower()
    if not (
        normalized_remote.endswith(f"github.com/{repository_lower}")
        or normalized_remote.endswith(f"github.com:{repository_lower}")
    ):
        return None

    sha = completed.stdout.strip()
    if not _SHA.fullmatch(sha):
        return None
    return sha.lower()


def runtime_binding_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return sanitized execution telemetry; it never grants mutation authority."""

    source = os.environ if env is None else env
    repository = _bounded_env_text(source, "GITHUB_REPOSITORY", limit=200)
    trigger_sha = _bounded_env_text(source, "GITHUB_SHA", limit=40)
    github_actions = str(source.get("GITHUB_ACTIONS") or "").strip().lower() == "true"

    if (
        not github_actions
        or repository is None
        or trigger_sha is None
        or not _REPOSITORY.fullmatch(repository)
        or not _SHA.fullmatch(trigger_sha)
    ):
        return {
            "status": "UNBOUND",
            "source": "GITHUB_ACTIONS_RUNTIME_ENV",
            "telemetry_grants_no_authority": True,
        }

    explicit_exact_present = "UES_EXACT_RUNTIME_SHA" in source
    exact_runtime_sha = _bounded_env_text(source, "UES_EXACT_RUNTIME_SHA", limit=40)
    binding_source = "GITHUB_ACTIONS_TRIGGER_ENV"
    runtime_sha = trigger_sha.lower()

    if explicit_exact_present:
        if exact_runtime_sha is None or not _SHA.fullmatch(exact_runtime_sha):
            return {
                "status": "UNBOUND",
                "source": "UES_EXACT_RUNTIME_ENV",
                "trigger_sha": trigger_sha.lower(),
                "telemetry_grants_no_authority": True,
            }
        runtime_sha = exact_runtime_sha.lower()
        binding_source = "UES_EXACT_RUNTIME_ENV"
    elif env is None:
        checked_out_sha = _checked_out_runtime_sha(repository)
        if checked_out_sha is not None:
            runtime_sha = checked_out_sha
            binding_source = "CHECKED_OUT_GIT_HEAD"

    result: dict[str, Any] = {
        "status": "BOUND",
        "repository": repository,
        "sha": runtime_sha,
        "trigger_sha": trigger_sha.lower(),
        "source": binding_source,
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


def _telemetry_degraded_result(
    *,
    project: str,
    route: str,
    status: str,
    binding: Mapping[str, Any],
    health_durable: bool,
    runtime_binding_durable: bool,
    error: BaseException | None,
    original_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return explicit non-authoritative telemetry degradation without granting authority.

    Lifecycle health/runtime-binding lanes are observability only. Their write
    failure must not become a project-effect gate because every downstream
    mutation still has independent Current Authority, exact binding,
    duplicate/UNKNOWN/idempotency and authoritative StateStore transition gates.
    """

    result = dict(original_result or {})
    result.setdefault("lane_id", canonical_lane_id(project, route, legacy.HEALTH_WORKSTREAM))
    result.setdefault("version", None)
    result["status"] = status
    result["health_telemetry_durable"] = bool(health_durable)
    result["runtime_binding_durable"] = bool(runtime_binding_durable)
    result["telemetry_grants_no_authority"] = True
    result["telemetry_failure_blocks_downstream_effects"] = False
    result["downstream_authority_and_state_gates_required"] = True
    result["safe_to_blind_retry"] = False
    if error is not None:
        result["telemetry_error_category"] = type(error).__name__
    result["runtime_binding_status"] = str(binding.get("status") or "UNBOUND")
    if binding.get("status") == "BOUND":
        result["runtime_sha"] = binding.get("sha")
    return result


def _persist_health_with_runtime_binding(
    original: Callable[..., dict[str, Any]],
    runtime_binding: Mapping[str, Any],
) -> Callable[..., dict[str, Any]]:
    """Decorate best-effort health writes with exact sanitized runtime binding.

    Health and runtime-binding records are telemetry only. A bounded StateStore
    conflict/unavailability here is recorded as degraded observability and the
    already-started lifecycle continues into its authoritative downstream gates.
    Provider or project effects are never retried or authorized by this wrapper.
    """

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
        try:
            original_result = original(
                store,
                project=project,
                route=route,
                status=status,
                summary=summary,
                error_category=error_category,
            )
        except _TELEMETRY_WRITE_ERRORS as exc:
            return _telemetry_degraded_result(
                project=project,
                route=route,
                status=status,
                binding=binding,
                health_durable=False,
                runtime_binding_durable=False,
                error=exc,
            )

        lane_id = canonical_lane_id(project, route, legacy.HEALTH_WORKSTREAM)
        try:
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
        except _TELEMETRY_WRITE_ERRORS as exc:
            return _telemetry_degraded_result(
                project=project,
                route=route,
                status=status,
                binding=binding,
                health_durable=True,
                runtime_binding_durable=False,
                error=exc,
                original_result=original_result,
            )

        result = dict(original_result)
        result["version"] = saved.version
        result["runtime_binding_status"] = str(binding.get("status") or "UNBOUND")
        result["health_telemetry_durable"] = True
        result["runtime_binding_durable"] = True
        result["telemetry_grants_no_authority"] = True
        result["telemetry_failure_blocks_downstream_effects"] = False
        result["downstream_authority_and_state_gates_required"] = True
        result["safe_to_blind_retry"] = False
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


def _is_pre_effect_provider_read_failure(exc: BaseException) -> bool:
    return str(getattr(exc, "operation", "") or "") in _PRE_EFFECT_PROVIDER_READ_OPERATIONS


def _provider_read_unavailable_result(project: str, exc: BaseException) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project": str(project).upper(),
        "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
        "lifecycle_state": "WAITING",
        "provider_read_authoritative": False,
        "provider_read_operation": str(getattr(exc, "operation", "") or "UNKNOWN"),
        "provider_read_error_category": str(getattr(exc, "category", "") or type(exc).__name__),
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "retry_condition": "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED",
        "safe_to_blind_retry": False,
        "raw_session_ids_persisted": False,
    }


def run(project: str) -> dict[str, Any]:
    runtime_binding = runtime_binding_from_env()
    original = legacy._persist_health
    legacy._persist_health = _persist_health_with_runtime_binding(original, runtime_binding)
    try:
        try:
            result = dict(current.run(project))
        except _PRE_EFFECT_PROVIDER_READ_ERRORS as exc:
            if not _is_pre_effect_provider_read_failure(exc):
                raise
            result = _provider_read_unavailable_result(project, exc)
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
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    if result.get("result") == _PROVIDER_READ_UNAVAILABLE_RESULT:
        return _PROVIDER_READ_UNAVAILABLE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
