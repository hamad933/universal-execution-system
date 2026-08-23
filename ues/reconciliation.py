from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Iterable, Mapping, Optional

from .lifecycle import (
    CIOutcome, FailureClass, LifecycleContext, LifecycleResolution, LifecycleState,
    NextAction, ReviewOutcome, StopGate, WaitingClass, ensure_lifecycle_resolution,
    resolve_next_action,
)

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class CIBinding:
    run_id: Optional[str] = None
    job_id: Optional[str] = None
    artifact_id: Optional[str] = None
    candidate_sha: Optional[str] = None
    outcome: Optional[CIOutcome] = None


@dataclass(frozen=True)
class ReviewBinding:
    review_id: Optional[str] = None
    reviewed_sha: Optional[str] = None
    reviewer_lineage: Optional[str] = None
    outcome: Optional[ReviewOutcome] = None
    stale: bool = False
    stale_reason: Optional[str] = None


@dataclass(frozen=True)
class WorkstreamBinding:
    project: Optional[str]
    route: Optional[str]
    workstream: Optional[str]
    role: Optional[str]
    repo: Optional[str]
    branch: Optional[str]
    lifecycle_state: LifecycleState
    baseline_sha: Optional[str]
    base_ref: Optional[str]
    task_budget_class: Optional[str]
    last_activity_at: Optional[datetime]
    jules_session_id: Optional[str] = None
    jules_task_id: Optional[str] = None
    writer_lineage: Optional[str] = None
    reviewer_lineage: Optional[str] = None
    pr_number: Optional[int] = None
    head_sha: Optional[str] = None
    ci: Optional[CIBinding] = None
    review: Optional[ReviewBinding] = None
    waiting_class: Optional[WaitingClass] = None
    error_class: Optional[FailureClass] = None
    resume_state: Optional[LifecycleState] = None
    action_in_flight: Optional[str] = None
    lease_id: Optional[str] = None
    operation_key: Optional[str] = None
    receipt_id: Optional[str] = None
    next_action: Optional[str] = None
    stop_gate: Optional[str] = None


@dataclass(frozen=True)
class ReconciliationResult:
    binding: WorkstreamBinding
    resolution: LifecycleResolution
    issues: tuple[str, ...]
    candidate_sha_moved: bool = False
    prior_review_invalidated: bool = False

    @property
    def executable(self) -> bool:
        return self.resolution.executable and not self.issues and self.binding.action_in_flight is None


def _text(value: Optional[str]) -> Optional[str]:
    return value.strip() or None if value is not None else None


def _sha(value: Optional[str]) -> bool:
    return bool(value and _FULL_SHA.fullmatch(value))


def _structural_issues(binding: WorkstreamBinding) -> list[str]:
    issues = []
    identity = {
        "project": binding.project, "route": binding.route, "workstream": binding.workstream,
        "role": binding.role, "repo": binding.repo, "branch": binding.branch,
        "base_ref": binding.base_ref, "task_budget_class": binding.task_budget_class,
    }
    issues += [f"missing:{name}" for name, value in identity.items() if _text(value) is None]
    if not _sha(binding.baseline_sha):
        issues.append("invalid_or_missing:baseline_sha")
    if binding.head_sha is not None and not _sha(binding.head_sha):
        issues.append("invalid:head_sha")
    if binding.ci and binding.ci.candidate_sha is not None and not _sha(binding.ci.candidate_sha):
        issues.append("invalid:ci.candidate_sha")
    if binding.review and binding.review.reviewed_sha is not None and not _sha(binding.review.reviewed_sha):
        issues.append("invalid:review.reviewed_sha")
    if binding.pr_number is not None and binding.pr_number <= 0:
        issues.append("invalid:pr_number")
    if binding.last_activity_at is None:
        issues.append("missing:last_activity_at")
    elif binding.last_activity_at.tzinfo is None or binding.last_activity_at.utcoffset() is None:
        issues.append("invalid:last_activity_at_must_be_timezone_aware")
    if _text(binding.action_in_flight):
        if _text(binding.lease_id) is None:
            issues.append("missing:lease_id_for_action_in_flight")
        if _text(binding.operation_key) is None:
            issues.append("missing:operation_key_for_action_in_flight")
    return issues


def _action_issues(binding: WorkstreamBinding, action: NextAction) -> list[str]:
    issues = []
    exact_head_actions = {
        NextAction.RUN_EXACT_HEAD_CI, NextAction.CLASSIFY_CI, NextAction.START_EXACT_SHA_REVIEW,
        NextAction.CAPTURE_REVIEW_RESULT, NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
        NextAction.CONTINUE_SAME_WRITER, NextAction.VERIFY_CANDIDATE_SHA,
        NextAction.INVALIDATE_PRIOR_REVIEW, NextAction.START_RE_REVIEW,
        NextAction.REQUEST_PARENT_REVIEW, NextAction.RECONCILE_CI_EVIDENCE,
        NextAction.RECONCILE_REVIEW_EVIDENCE,
    }
    if action in exact_head_actions and not _sha(binding.head_sha):
        issues.append("missing_exact:head_sha")
    if action in {NextAction.RUN_EXACT_HEAD_CI, NextAction.START_EXACT_SHA_REVIEW,
                  NextAction.CAPTURE_REVIEW_RESULT, NextAction.REQUEST_PARENT_REVIEW,
                  NextAction.RECONCILE_REVIEW_EVIDENCE} and binding.pr_number is None:
        issues.append("missing:pr_number")
    if action in {NextAction.CLASSIFY_CI, NextAction.RECONCILE_CI_EVIDENCE}:
        if binding.ci is None or _text(binding.ci.run_id) is None:
            issues.append("missing:ci.run_id")
        elif binding.ci.candidate_sha and binding.head_sha and binding.ci.candidate_sha.lower() != binding.head_sha.lower():
            issues.append("mismatch:ci.candidate_sha")
    if action in {NextAction.ROUTE_FINDINGS_TO_SAME_WRITER, NextAction.CONTINUE_SAME_WRITER} and _text(binding.writer_lineage) is None:
        issues.append("missing:writer_lineage")
    if action in {NextAction.START_EXACT_SHA_REVIEW, NextAction.START_RE_REVIEW} and _text(binding.reviewer_lineage) is None:
        issues.append("missing:reviewer_lineage")
    if action in {NextAction.CAPTURE_REVIEW_RESULT, NextAction.REQUEST_PARENT_REVIEW, NextAction.RECONCILE_REVIEW_EVIDENCE}:
        if binding.review is None or not _sha(binding.review.reviewed_sha):
            issues.append("missing_exact:review.reviewed_sha")
        elif binding.head_sha and binding.review.reviewed_sha.lower() != binding.head_sha.lower():
            issues.append("mismatch:review.reviewed_sha")
        if binding.review and binding.review.stale:
            issues.append("stale:review")
    return issues


def _context(binding: WorkstreamBinding) -> LifecycleContext:
    return LifecycleContext(
        state=binding.lifecycle_state,
        review_outcome=binding.review.outcome if binding.review else None,
        ci_outcome=binding.ci.outcome if binding.ci else None,
        waiting_class=binding.waiting_class,
        failure_class=binding.error_class,
        resume_state=binding.resume_state,
        review_stale=bool(binding.review and binding.review.stale),
    )


def _invalidate_sha_move(current: WorkstreamBinding, previous: Optional[WorkstreamBinding]):
    if not previous or not previous.head_sha or not current.head_sha or previous.head_sha.lower() == current.head_sha.lower():
        return current, False, False
    review = current.review or previous.review
    invalidated = bool(review and review.reviewed_sha)
    if review:
        review = replace(review, stale=True, stale_reason=f"candidate SHA moved from {previous.head_sha} to {current.head_sha}")
    return replace(current, lifecycle_state=LifecycleState.NEW_SHA, review=review), True, invalidated


def _invalidate_mismatch(binding: WorkstreamBinding):
    review = binding.review
    if not review or not review.reviewed_sha or not binding.head_sha or review.reviewed_sha.lower() == binding.head_sha.lower():
        return binding, False
    review = replace(review, stale=True, stale_reason=review.stale_reason or f"reviewed SHA {review.reviewed_sha} != candidate {binding.head_sha}")
    state = LifecycleState.PRIOR_REVIEW_STALE if binding.lifecycle_state in {
        LifecycleState.REVIEW_RESULT, LifecycleState.PARENT_REVIEW_PENDING, LifecycleState.REVIEWER_ACTIVE
    } else binding.lifecycle_state
    return replace(binding, review=review, lifecycle_state=state), True


def _blocked(binding: WorkstreamBinding, issues: Iterable[str], gate: StopGate = StopGate.INCOMPLETE_BINDING):
    items = tuple(dict.fromkeys(issues))
    resolution = ensure_lifecycle_resolution(binding.lifecycle_state, stop_gate=gate, reason="; ".join(items))
    bound = replace(binding, next_action=None, stop_gate=gate.value)
    return ReconciliationResult(bound, resolution, items)


def reconcile_workstream(binding: WorkstreamBinding, previous: Optional[WorkstreamBinding] = None) -> ReconciliationResult:
    structural = _structural_issues(binding)
    if structural:
        return _blocked(binding, structural)
    current, moved, invalidated_move = _invalidate_sha_move(binding, previous)
    current, invalidated_mismatch = _invalidate_mismatch(current)
    invalidated = invalidated_move or invalidated_mismatch
    if _text(current.action_in_flight):
        resolution = ensure_lifecycle_resolution(
            current.lifecycle_state, action=NextAction.VERIFY_ACTION_IN_FLIGHT,
            reason="reconcile authoritative post-state before any duplicate action",
        )
    else:
        resolution = resolve_next_action(_context(current))
    action_issues = _action_issues(current, resolution.action) if resolution.action else []
    if action_issues:
        result = _blocked(current, action_issues)
        return replace(result, candidate_sha_moved=moved, prior_review_invalidated=invalidated)
    bound = replace(
        current,
        next_action=resolution.action.value if resolution.action else None,
        stop_gate=resolution.stop_gate.value if resolution.stop_gate else None,
    )
    return ReconciliationResult(bound, resolution, (), moved, invalidated)


def reconcile_portfolio(
    bindings: Iterable[WorkstreamBinding],
    previous_by_workstream: Optional[Mapping[str, WorkstreamBinding]] = None,
) -> tuple[ReconciliationResult, ...]:
    items = tuple(bindings)
    counts = Counter(item.workstream for item in items if item.workstream)
    previous_by_workstream = previous_by_workstream or {}
    results = []
    for item in items:
        if item.workstream and counts[item.workstream] > 1:
            results.append(_blocked(item, ["ambiguous:duplicate_workstream"], StopGate.AMBIGUOUS_WORKSTREAM_BINDING))
        else:
            results.append(reconcile_workstream(item, previous_by_workstream.get(item.workstream or "")))
    return tuple(results)
