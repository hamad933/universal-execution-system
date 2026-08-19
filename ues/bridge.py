from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import load_contract, resolve_adapter_plan
from .authority_transport import derive_owner_comment_authority
from .cli import detect, doctor, evidence, preflight, status
from .failures import classify_failure, scope_blocker
from .idempotency import evaluate_write_boundary, make_operation_receipt
from .recovery import reconcile_checkpoint
from .transaction import plan_mutation


READ_ONLY_COMMANDS = {
    "adapter-plan",
    "capabilities",
    "detect",
    "doctor",
    "evidence",
    "failure-classify",
    "mutation-authorize",
    "mutation-plan",
    "preflight",
    "reconcile",
    "status",
}

WRITE_COMMANDS = {
    "apply-change-set",
    "commit",
    "create-branch",
    "create-pr",
    "format-fix",
    "push",
}


@dataclass(frozen=True)
class ExecRequest:
    command: str
    arguments: dict[str, str]


def parse_exec_request(comment: str) -> ExecRequest:
    first_line = next((line.strip() for line in comment.splitlines() if line.strip()), "")
    if not first_line.startswith("/exec"):
        raise ValueError("comment does not start with /exec")
    parts = shlex.split(first_line)
    if len(parts) < 2:
        raise ValueError("missing /exec command")
    command = parts[1]
    if command in WRITE_COMMANDS:
        raise ValueError(f"write command not enabled in read-only bridge: {command}")
    if command not in READ_ONLY_COMMANDS:
        raise ValueError(f"unsupported read-only command: {command}")
    arguments: dict[str, str] = {}
    for token in parts[2:]:
        if "=" not in token:
            raise ValueError(f"arguments must use key=value syntax: {token}")
        key, value = token.split("=", 1)
        if not key or key in arguments:
            raise ValueError(f"invalid or duplicate argument: {key}")
        arguments[key.replace("-", "_")] = value
    return ExecRequest(command=command, arguments=arguments)


def _only(arguments: dict[str, str], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unsupported arguments: {', '.join(unknown)}")


def _repo_relative_path(repo: Path, value: str) -> Path:
    root = repo.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes repository root") from exc
    return candidate


def execute_readonly_request(
    request: ExecRequest,
    repo: Path,
    *,
    repository: str,
    workstream_id: str,
    operation_id: str,
    default_expected_sha: str | None = None,
    default_ref: str | None = None,
    authority_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = request.arguments
    command = request.command

    if command in {"capabilities", "detect"}:
        _only(args, set())
        return {"bridge_command": command, "result": detect(repo)}

    if command == "adapter-plan":
        _only(args, {"contract"})
        contract = None
        if "contract" in args:
            contract = load_contract(_repo_relative_path(repo, args["contract"]))
        return {
            "bridge_command": command,
            "result": resolve_adapter_plan(
                repo,
                detect(repo)["capabilities"],
                contract=contract,
            ),
        }

    if command == "failure-classify":
        _only(args, {"input", "workstream"})
        if "input" not in args:
            raise ValueError("failure-classify requires input=<repository-relative JSON path>")
        failure = load_contract(_repo_relative_path(repo, args["input"]))
        classification = classify_failure(failure)
        return {
            "bridge_command": command,
            "result": {
                "classification": classification,
                "blocker_scope": scope_blocker(
                    classification,
                    args.get("workstream") or workstream_id,
                ),
            },
        }

    if command == "mutation-authorize":
        _only(args, {"operation", "sha", "ref", "paths", "resources", "max_paths"})
        if authority_context is None:
            raise ValueError("mutation-authorize requires trusted authority event context")
        snapshot = status(repo)
        candidate_ref = default_ref or str(authority_context.get("candidate_ref") or "")
        transport = derive_owner_comment_authority(
            args,
            actor=str(authority_context.get("actor") or ""),
            repository_owner=str(authority_context.get("repository_owner") or ""),
            repository=repository,
            pr_number=int(authority_context.get("pr_number") or 0),
            comment_id=str(authority_context.get("event_id") or ""),
            comment_created_at=str(authority_context.get("issued_at") or ""),
            candidate_ref=candidate_ref,
            candidate_head_sha=snapshot["head_sha"],
            candidate_tree_sha=snapshot["tree_sha"],
            workstream_id=workstream_id,
        )
        derived_operation_id = transport["operation_id"]
        plan = plan_mutation(
            transport["authority_envelope"],
            transport["mutation_request"],
            repository=repository,
            ref=candidate_ref,
            live_head_sha=snapshot["head_sha"],
            live_tree_sha=snapshot["tree_sha"],
            operation_id=derived_operation_id,
            workstream_id=workstream_id,
            active_leases=[],
        )
        records = authority_context.get("operation_records") or []
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise ValueError("trusted operation_records must be an array of objects")
        boundary = evaluate_write_boundary(
            mutation_plan=plan,
            operation_id=derived_operation_id,
            request_digest=transport["request_digest"],
            repository=repository,
            ref=candidate_ref,
            live_head_sha=snapshot["head_sha"],
            live_tree_sha=snapshot["tree_sha"],
            operation_records=records,
        )
        receipt = make_operation_receipt(
            operation_id=derived_operation_id,
            request_digest=transport["request_digest"],
            repository=repository,
            ref=candidate_ref,
            authority_event_id=str(authority_context.get("event_id") or ""),
            start_sha=snapshot["head_sha"],
            start_tree_sha=snapshot["tree_sha"],
        ) if boundary["ready"] else None
        return {
            "bridge_command": command,
            "result": {
                "transport": transport,
                "mutation_plan": plan,
                "write_boundary": boundary,
                "preview_receipt": receipt,
                "execution_enabled": False,
            },
        }

    if command == "mutation-plan":
        _only(args, {"authority", "request", "leases"})
        if "authority" not in args or "request" not in args:
            raise ValueError("mutation-plan requires authority=<path> request=<path>")
        authority = load_contract(_repo_relative_path(repo, args["authority"]))
        mutation_request = load_contract(_repo_relative_path(repo, args["request"]))
        active_leases: list[dict[str, Any]] = []
        if "leases" in args:
            lease_document = load_contract(_repo_relative_path(repo, args["leases"]))
            raw_leases = lease_document.get("leases", [])
            if not isinstance(raw_leases, list) or not all(isinstance(item, dict) for item in raw_leases):
                raise ValueError("leases document must contain a leases array of objects")
            active_leases = raw_leases
        snapshot = status(repo)
        return {
            "bridge_command": command,
            "result": plan_mutation(
                authority,
                mutation_request,
                repository=repository,
                ref=default_ref or snapshot["branch"],
                live_head_sha=snapshot["head_sha"],
                live_tree_sha=snapshot["tree_sha"],
                active_leases=active_leases,
            ),
        }

    if command == "reconcile":
        _only(args, {"checkpoint"})
        if "checkpoint" not in args:
            raise ValueError("reconcile requires checkpoint=<repository-relative JSON path>")
        checkpoint = load_contract(_repo_relative_path(repo, args["checkpoint"]))
        return {
            "bridge_command": command,
            "result": reconcile_checkpoint(checkpoint, status(repo)["head_sha"]),
        }

    if command == "status":
        _only(args, set())
        return {"bridge_command": command, "result": status(repo)}

    if command == "doctor":
        _only(args, set())
        return {"bridge_command": command, "result": doctor(repo)}

    if command == "preflight":
        _only(args, {"sha", "branch", "allow_dirty"})
        expected_sha = args.get("sha") or default_expected_sha
        if not expected_sha:
            raise ValueError("preflight requires sha=<expected SHA>")
        allow_dirty = args.get("allow_dirty", "false").lower() in {"1", "true", "yes"}
        return {
            "bridge_command": command,
            "result": preflight(
                repo,
                expected_sha=expected_sha,
                expected_branch=args.get("branch"),
                require_clean=not allow_dirty,
            ),
        }

    if command == "evidence":
        _only(args, {"start_sha", "base_sha"})
        start_sha = args.get("start_sha") or default_expected_sha
        if not start_sha:
            raise ValueError("evidence requires start_sha=<SHA>")
        return {
            "bridge_command": command,
            "result": evidence(
                repo,
                repository=repository,
                workstream_id=workstream_id,
                operation_id=operation_id,
                start_sha=start_sha,
                base_sha=args.get("base_sha"),
            ),
        }

    raise ValueError(f"unsupported read-only command: {command}")
