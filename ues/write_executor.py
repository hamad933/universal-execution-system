from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .authority_transport import derive_owner_comment_authority
from .idempotency import evaluate_write_boundary, make_operation_receipt
from .operation_records import trusted_operation_records
from .transaction import plan_mutation


def parse_format_fix_comment(comment: str) -> dict[str, str]:
    first_line = next((line.strip() for line in comment.splitlines() if line.strip()), "")
    parts = shlex.split(first_line)
    if len(parts) < 2 or parts[0] != "/exec" or parts[1] != "format-fix":
        raise ValueError("write workflow only accepts /exec format-fix")
    arguments: dict[str, str] = {"operation": "format-fix"}
    for token in parts[2:]:
        if "=" not in token:
            raise ValueError(f"write arguments must use key=value syntax: {token}")
        key, value = token.split("=", 1)
        key = key.replace("-", "_")
        if not key or key in arguments:
            raise ValueError(f"invalid or duplicate write argument: {key}")
        arguments[key] = value
    allowed = {"operation", "sha", "ref", "paths", "max_paths", "formatter"}
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unsupported format-fix arguments: {', '.join(unknown)}")
    return arguments


def _receipt(
    trusted: dict[str, Any],
    *,
    state: str,
    start_sha: str,
    start_tree_sha: str | None,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = trusted["authority_envelope"]
    request = trusted["mutation_request"]
    receipt = make_operation_receipt(
        operation_id=trusted["operation_id"],
        request_digest=trusted["request_digest"],
        repository=envelope["repository"],
        ref=envelope["ref"],
        authority_event_id=trusted["authority_event"]["event_id"],
        start_sha=start_sha,
        start_tree_sha=start_tree_sha,
        state=state,
    )
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    receipt["schema_version"] = "0.6"
    receipt["extensions"] = {
        "operation": request["operation"],
        "formatter": metadata.get("formatter"),
        "authorized_paths": request.get("proposed_paths", []),
        **(extensions or {}),
    }
    return receipt


def prepare_format_fix(
    comment: str,
    *,
    actor: str,
    repository_owner: str,
    repository: str,
    pr_number: int,
    comment_id: str,
    comment_created_at: str,
    candidate_ref: str,
    candidate_head_sha: str,
    candidate_tree_sha: str,
    workstream_id: str,
    prior_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    arguments = parse_format_fix_comment(comment)
    trusted = derive_owner_comment_authority(
        arguments,
        actor=actor,
        repository_owner=repository_owner,
        repository=repository,
        pr_number=pr_number,
        comment_id=comment_id,
        comment_created_at=comment_created_at,
        candidate_ref=candidate_ref,
        candidate_head_sha=candidate_head_sha,
        candidate_tree_sha=candidate_tree_sha,
        workstream_id=workstream_id,
    )
    records = trusted_operation_records(prior_comments)
    plan = plan_mutation(
        trusted["authority_envelope"],
        trusted["mutation_request"],
        repository=repository,
        ref=candidate_ref,
        live_head_sha=candidate_head_sha,
        live_tree_sha=candidate_tree_sha,
        operation_id=trusted["operation_id"],
        workstream_id=workstream_id,
    )
    boundary = evaluate_write_boundary(
        mutation_plan=plan,
        operation_id=trusted["operation_id"],
        request_digest=trusted["request_digest"],
        repository=repository,
        ref=candidate_ref,
        live_head_sha=candidate_head_sha,
        live_tree_sha=candidate_tree_sha,
        operation_records=records,
    )

    idem = boundary["idempotency"]
    if idem["decision"] != "NEW_OPERATION":
        return {
            "schema_version": "0.6",
            "decision": idem["decision"],
            "should_execute": False,
            "publish_receipt": False,
            "operation_id": trusted["operation_id"],
            "start_sha": candidate_head_sha,
            "start_tree_sha": candidate_tree_sha,
            "trusted_authority": trusted,
            "mutation_plan": plan,
            "write_boundary": boundary,
        }

    if not boundary["ready"]:
        rejected = _receipt(
            trusted,
            state="REJECTED",
            start_sha=candidate_head_sha,
            start_tree_sha=candidate_tree_sha,
            extensions={"phase": "authorization", "failures": boundary["failures"]},
        )
        return {
            "schema_version": "0.6",
            "decision": "REJECTED",
            "should_execute": False,
            "publish_receipt": True,
            "operation_id": trusted["operation_id"],
            "start_sha": candidate_head_sha,
            "start_tree_sha": candidate_tree_sha,
            "trusted_authority": trusted,
            "mutation_plan": plan,
            "write_boundary": boundary,
            "receipt": rejected,
        }

    planned = _receipt(
        trusted,
        state="PLANNED",
        start_sha=candidate_head_sha,
        start_tree_sha=candidate_tree_sha,
        extensions={"phase": "authorized"},
    )
    return {
        "schema_version": "0.6",
        "decision": "AUTHORIZED_FOR_SANDBOX_FORMAT",
        "should_execute": True,
        "publish_receipt": True,
        "operation_id": trusted["operation_id"],
        "start_sha": candidate_head_sha,
        "start_tree_sha": candidate_tree_sha,
        "trusted_authority": trusted,
        "mutation_plan": plan,
        "write_boundary": boundary,
        "receipt": planned,
    }


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=check, text=True, capture_output=True
    )
    return completed.stdout.strip()


def _remote_head(repo: Path, ref: str) -> str | None:
    target = f"refs/heads/{ref}"
    output = _git(repo, "ls-remote", "--heads", "origin", target)
    if not output:
        return None
    line = output.splitlines()[0]
    return line.split()[0]


def _clean_paths(repo: Path) -> list[str]:
    output = _git(repo, "status", "--porcelain=v1")
    return [line for line in output.splitlines() if line]


def apply_format_patch(
    prepared: dict[str, Any],
    format_result: dict[str, Any],
    patch: bytes,
    repo: Path,
) -> dict[str, Any]:
    if not prepared.get("should_execute"):
        raise ValueError("prepared operation is not executable")
    trusted = prepared["trusted_authority"]
    request = trusted["mutation_request"]
    envelope = trusted["authority_envelope"]
    if request.get("operation") != "format-fix":
        raise ValueError("write executor only supports format-fix")

    start_sha = str(prepared["start_sha"])
    start_tree = prepared.get("start_tree_sha")
    ref = str(envelope["ref"])
    local_head = _git(repo, "rev-parse", "HEAD")
    local_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if local_head != start_sha or (start_tree and local_tree != start_tree):
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={"phase": "apply", "failure": "LOCAL_HEAD_OR_TREE_MOVED"},
        )
    if _clean_paths(repo):
        raise ValueError("apply worktree must start clean")

    remote_before = _remote_head(repo, ref)
    if remote_before != start_sha:
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "apply",
                "failure": "REMOTE_HEAD_MOVED_BEFORE_APPLY",
                "observed_remote_sha": remote_before,
            },
        )

    state = str(format_result.get("state") or "")
    if format_result.get("operation_id") != trusted["operation_id"]:
        raise ValueError("format artifact operation ID mismatch")
    if format_result.get("start_sha") != start_sha:
        raise ValueError("format artifact start SHA mismatch")

    authorized_paths = [str(item) for item in request.get("proposed_paths", [])]
    changed_paths = [str(item) for item in format_result.get("changed_paths", [])]
    if sorted(set(changed_paths) - set(authorized_paths)):
        raise ValueError("format artifact contains unauthorized changed paths")

    expected_patch_sha = str(format_result.get("patch_sha256") or "")
    actual_patch_sha = hashlib.sha256(patch).hexdigest()
    if expected_patch_sha != actual_patch_sha:
        raise ValueError("format patch digest mismatch")

    if state == "NO_CHANGE":
        receipt = _receipt(
            trusted,
            state="CONFIRMED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "complete",
                "result": "NO_CHANGE_REQUIRED",
                "changed_paths": [],
                "patch_sha256": actual_patch_sha,
            },
        )
        receipt["final_sha"] = start_sha
        receipt["final_tree_sha"] = start_tree
        return receipt
    if state != "FORMAT_READY":
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={"phase": "format", "failure": state or "FORMAT_RESULT_INVALID"},
        )
    if not patch or not changed_paths:
        raise ValueError("FORMAT_READY artifact requires non-empty patch and changed paths")

    patch_path = repo / ".git" / "ues-format.patch"
    patch_path.write_bytes(patch)
    try:
        subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=repo, check=True)
        subprocess.run(["git", "apply", str(patch_path)], cwd=repo, check=True)
    finally:
        patch_path.unlink(missing_ok=True)

    observed = _git(repo, "diff", "--name-only", "--no-ext-diff").splitlines()
    observed = [item for item in observed if item]
    if sorted(observed) != sorted(changed_paths):
        raise ValueError("post-apply changed paths do not match sandbox artifact")
    if sorted(set(observed) - set(authorized_paths)):
        raise ValueError("post-apply verification found unauthorized paths")

    subprocess.run(["git", "add", "--", *observed], cwd=repo, check=True)
    _git(repo, "config", "user.name", "github-actions[bot]")
    _git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    subprocess.run(
        ["git", "commit", "-m", f"ues(format-fix): {trusted['operation_id']}"],
        cwd=repo,
        check=True,
    )
    new_sha = _git(repo, "rev-parse", "HEAD")
    new_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    remote_pre_push = _remote_head(repo, ref)
    if remote_pre_push != start_sha:
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "pre-push",
                "failure": "REMOTE_HEAD_MOVED_AT_WRITE_BOUNDARY",
                "observed_remote_sha": remote_pre_push,
                "local_candidate_sha": new_sha,
            },
        )

    push = subprocess.run(
        ["git", "push", "origin", f"HEAD:refs/heads/{ref}"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    remote_after = _remote_head(repo, ref)
    if remote_after == new_sha:
        receipt = _receipt(
            trusted,
            state="CONFIRMED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "complete",
                "result": "FORMAT_FIX_APPLIED",
                "changed_paths": observed,
                "patch_sha256": actual_patch_sha,
                "push_returncode": push.returncode,
            },
        )
        receipt["final_sha"] = new_sha
        receipt["final_tree_sha"] = new_tree
        return receipt

    if remote_after == start_sha:
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "push",
                "failure": "PUSH_NOT_OBSERVED",
                "local_candidate_sha": new_sha,
                "push_returncode": push.returncode,
                "stderr": push.stderr[-2000:],
            },
        )

    return _receipt(
        trusted,
        state="UNKNOWN",
        start_sha=start_sha,
        start_tree_sha=start_tree,
        extensions={
            "phase": "push",
            "failure": "REMOTE_POST_STATE_DIVERGED",
            "local_candidate_sha": new_sha,
            "observed_remote_sha": remote_after,
            "push_returncode": push.returncode,
        },
    )


def fallback_final_receipt(
    prepared: dict[str, Any],
    *,
    format_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trusted = prepared["trusted_authority"]
    start_sha = str(prepared["start_sha"])
    start_tree = prepared.get("start_tree_sha")
    if format_result is not None and format_result.get("state") == "FORMAT_FAILED":
        return _receipt(
            trusted,
            state="REJECTED",
            start_sha=start_sha,
            start_tree_sha=start_tree,
            extensions={
                "phase": "format",
                "failure": "FORMATTER_FAILED",
                "stderr": str(format_result.get("stderr") or "")[-2000:],
            },
        )
    return _receipt(
        trusted,
        state="UNKNOWN",
        start_sha=start_sha,
        start_tree_sha=start_tree,
        extensions={
            "phase": "finalize",
            "failure": "WRITE_OUTCOME_NOT_DURABLY_CONFIRMED",
        },
    )
