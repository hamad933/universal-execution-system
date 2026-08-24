from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import provider_observer as provider_observer
from . import provider_observer_runtime as provider_runtime
from .observation_backed_health import run_observation_backed_no_effect_health


RP_PROJECTS: tuple[dict[str, str], ...] = (
    {"project": "RP01", "route": "RP01", "repository": "hamad933/Bayt-Style"},
    {"project": "RP02", "route": "RP02", "repository": "hamad933/Enterprise-Operations-Control"},
    {"project": "RP03", "route": "RP03", "repository": "hamad933/BOOKING-SERVICES"},
    {"project": "RP04", "route": "RP04", "repository": "hamad933/Real-Estate-Assets-Control-"},
)
RP_NAMES = frozenset(project["project"] for project in RP_PROJECTS)


def _load_rp_adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().upper()
    if name not in RP_NAMES:
        raise ValueError("project must be RP01, RP02, RP03, or RP04")
    path = Path(__file__).resolve().parents[1] / "adapters" / f"{name.lower()}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("project adapter must be an object")
    if value.get("project") != name or value.get("route") != name:
        raise ValueError("RP adapter identity mismatch")
    if value.get("activation", {}).get("default_mode") != "SHADOW":
        raise ValueError("RP read-only runtime requires SHADOW adapter")
    if value.get("activation", {}).get("mutation_allowed") is not False:
        raise ValueError("RP read-only runtime rejects mutation-enabled adapter")
    return value


def _with_rp_observer_projects(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    original_observer = provider_observer.PROJECTS
    original_runtime = provider_runtime.PROJECTS
    provider_observer.PROJECTS = RP_PROJECTS
    provider_runtime.PROJECTS = RP_PROJECTS
    try:
        result = dict(action())
    finally:
        provider_observer.PROJECTS = original_observer
        provider_runtime.PROJECTS = original_runtime
    result["project_set"] = sorted(RP_NAMES)
    result["rp_runtime_mode"] = "READ_ONLY_SHADOW"
    result["provider_mutation_performed"] = False
    return result


def observe_provider() -> dict[str, Any]:
    """Observe only RP Jules sources/sessions through the shared GET-only observer."""

    return _with_rp_observer_projects(provider_runtime.observe)


def audit_provider(*, stale_seconds: int = 45 * 60) -> dict[str, Any]:
    return _with_rp_observer_projects(
        lambda: provider_observer.audit_provider_observation(stale_seconds=stale_seconds)
    )


def lifecycle_health(project: str) -> dict[str, Any]:
    """Persist RP SHADOW health from the provider-observer snapshot.

    The scheduled/push read-only workflow runs provider-observer first. With no
    Current Authority and no RP lineage topology there is no reason for each RP
    health job to re-read Jules. Reusing the fresh sanitized StateStore observation
    eliminates redundant provider load while preserving fail-closed freshness and
    exact repository binding. Any non-empty authority transport is rejected here;
    effect-capable RP cycles belong to rp_authority_runtime.
    """

    project_name = str(project or "").strip().upper()
    adapter = _load_rp_adapter(project_name)
    if str(os.environ.get("UES_CURRENT_AUTHORITY_JSON") or "").strip():
        raise ValueError("RP read-only lifecycle health rejects Current Authority transport")
    result = dict(run_observation_backed_no_effect_health(adapter, authority=None))
    result["rp_runtime_mode"] = "READ_ONLY_SHADOW"
    result["project"] = project_name
    result["runtime_wrapper_grants_authority"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES RP01-RP04 read-only SHADOW runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("observe-provider")
    audit = sub.add_parser("audit-provider")
    audit.add_argument("--stale-seconds", type=int, default=45 * 60)
    lifecycle = sub.add_parser("lifecycle-health")
    lifecycle.add_argument("project", choices=sorted(RP_NAMES))
    args = parser.parse_args(argv)

    if args.command == "observe-provider":
        result = observe_provider()
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("result") == "JULES_PROVIDER_OBSERVATION_COMPLETE" else 2
    if args.command == "audit-provider":
        result = audit_provider(stale_seconds=args.stale_seconds)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("cycle_status") == "PROVIDER_OBSERVER_OK" else 2

    result = lifecycle_health(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
