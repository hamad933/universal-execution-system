from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Iterable, Mapping, Optional

from .lifecycle import (
    AuthorizationDecision,
    CIOutcome,
    FailureClass,
    LifecycleContext,
    LifecycleResolution,
    LifecycleState,
    NextAction,
    ReviewOutcome,
    SourceBindingStatus,
    StopGate,
    WaitingClass,
    ensure_lifecycle_resolution,
    resolve_next_action,
)

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
LaneKey = tuple[str, str, str]


@dataclass(frozen=True)
class CIBinding:
    source_provider: Optional[str] = None
    source_repository: Optional[str] = None
    workflow_identity: Optional[str] = None
    required_check_identity: Optional[str] = None
    workflow_run_id: Optional[str] = None
    run_attempt: Optional[int] = None
    job_id: Optional[str] = None
    producer_job: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact_name: Optional[str] = None
    artifact_digest: Optional[str] = None
    candidate_sha: Optional[str] = None
    classification: Optional[str] = None
    outcome: Optional[CIOutcome] = None
    stale: bool = False
    stale_reason: Optional[str] = None


@dataclass(frozen=True)
class ReviewBinding:
    review_id: Optional[str] = None
    reviewed_sha: Optional[str] = None
    reviewer_lineage: Optional[str] = None
    source_repository: Optional[str] = None
    evidence_classification: Optional[str] = None
    outcome: Optional[ReviewOutcome] = None
    stale: bool = False
    stale_reason: Optional[str] = None


@dataclass(frozen=True)
class ProviderSourceBinding:
    provider: Optional[str] = None
    source_repository: Optional[str] = None
    source_identity: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    role: Optional[str] = None
    status: SourceBindingStatus = SourceBindingStatus.PROPOSED_UNVERIFIED
    evidence_id: Optional[str] = None

    @property
    def session_key(self) -> Optional[tuple[str, str]]:
        provider = _text(self.provider)
        session_id = _text(self.session_id)
        if provider is None or session_id is None:
            return None
        return provider, session_id


@dataclass(frozen=True)
class AuthorizationBinding:
    decision: AuthorizationDecision
    action: NextAction
    source: Optional[str] = None
    decision_id: Optional[str] = None


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
    provider_source: Optional[ProviderSourceBinding] = None
    authorization: Optional[AuthorizationBinding] = None
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

    @property
    def lane_key(self) -> Optional[LaneKey]:
        return canonical_lane_key(self)


@dataclass(frozen=True)
class ReconciliationResult:
    binding: WorkstreamBinding
    resolution: LifecycleResolution
    issues: tuple[str, ...]
    candidate_sha_moved: bool = False
    prior_review_invalidated: bool = False
    prior_ci_invalidated: bool = False

    @property
    def executable(self) -> bool:
        return (
            self.resolution.executable
            and not self.issues
            and self.binding.action_in_flight is None
        )


def _text(value: Optional[str]) -> Optional[str]:
    return value.strip() or None if value is not None else None


def _sha(value: Optional[str]) -> bool:
    return bool(value and _FULL_SHA.fullmatch(value))


def canonical_lane_key(binding: WorkstreamBinding) -> Optional[LaneKey]:
    project = _text(binding.project)
    route = _text(binding.route)
    workstream = _text(binding.workstream)
    if project is None or route is None or workstream is None:
        return None
    return project, route, workstream


def _structural_issues(binding: WorkstreamBinding) -> list[str]:
    issues: list[str] = []
    identity = {
        "project": binding.project,
        "route": binding.route,
        "workstream": binding.workstream,
        "role": binding.role,
        "repo": binding.repo,
        "branch": binding.branch,
        "base_ref": binding.base_ref,
        "task_budget_class": binding.task_budget_class,
    }
    issues += [
        f"missing:{name}"
        for name, value in identity.items()
        if _text(value) is None
    ]
    if not _sha(binding.baseline_sha):
        issues.append("invalid_or_missing:baseline_sha")
    if binding.head_sha is not None and not _sha(binding.head_sha):
        issues.append("invalid:head_sha")
    if binding.pr_number is not None and binding.pr_number <= 0:
        issues.append("invalid:pr_number")
    if binding.last_activity_at is None:
        issues.append("missing:last_activity_at")
    elif (
        binding.last_activity_at.tzinfo is None
        or binding.last_activity_at.utcoffset() is None
    ):
        issues.append("invalid:last_activity_at_must_be_timezone_aware")

    if binding.ci:
        if (
            binding.ci.candidate_sha is not None
            and not _sha(binding.ci.candidate_sha)
        ):
            issues.append("invalid:ci.candidate_sha")
        if binding.ci.run_attempt is not None and binding.ci.run_attempt <= 0:
            issues.append("invalid:ci.run_attempt")
        if (
            _text(binding.ci.source_repository)
            and _text(binding.repo)
            and binding.ci.source_repository != binding.repo
        ):
            issues.append("mismatch:ci.source_repository")
        if _text(binding.ci.artifact_id) and _text(binding.ci.producer_job) is None:
            issues.append("missing:ci.producer_job_for_artifact")

    if binding.review:
        if (
            binding.review.reviewed_sha is not None
            and not _sha(binding.review.reviewed_sha)
        ):
            issues.append("invalid:review.reviewed_sha")
        if (
            _text(binding.review.source_repository)
            and _text(binding.repo)
            and binding.review.source_repository != binding.repo
        ):
            issues.append("mismatch:review.source_repository")

    if binding.provider_source:
        provider = binding.provider_source
        if _text(provider.provider) is None:
            issues.append("missing:provider_source.provider")
        if _text(provider.source_repository) is None:
            issues.append("missing:provider_source.source_repository")
        elif _text(binding.repo) and provider.source_repository != binding.repo:
            issues.append("mismatch:provider_source.source_repository")
        if provider.status is SourceBindingStatus.PROVEN_EXPLICIT:
            if _text(provider.source_identity) is None:
                issues.append("missing:provider_source.source_identity_for_proof")
            if _text(provider.evidence_id) is None:
                issues.append("missing:provider_source.evidence_id_for_proof")
        if (
            _text(binding.jules_session_id)
            and _text(provider.session_id)
            and binding.jules_session_id != provider.session_id
        ):
            issues.append("mismatch:jules_session_id_vs_provider_source")
        if (
            _text(binding.jules_task_id)
            and _text(provider.task_id)
            and binding.jules_task_id != provider.task_id
        ):
            issues.append("mismatch:jules_task_id_vs_provider_source")

    if binding.authorization:
        authorization = binding.authorization
        if _text(authorization.source) is None:
            issues.append("missing:authorization.source")
        if _text(authorization.decision_id) is None:
            issues.append("missing:authorization.decision_id")

    if _text(binding.action_in_flight):
        if _text(binding.lease_id) is None:
            issues.append("missing:lease_id_for_action_in_flight")
        if _text(binding.operation_key) is None:
            issues.append("missing:operation_key_for_action_in_flight")
    return issues


def _ci_evidence_issues(binding: WorkstreamBinding) -> list[str]:
    issues: list[str] = []
    ci = binding.ci
    if ci is None:
        return ["missing:ci"]
    required = {
        "ci.source_provider": ci.source_provider,
        "ci.source_repository": ci.source_repository,
        "ci.workflow_identity": ci.workflow_identity,
        "ci.required_check_identity": ci.required_check_identity,
        "ci.workflow_run_id": ci.workflow_run_id,
        "ci.candidate_sha": ci.candidate_sha,
        "ci.classification": ci.classification,
    }
    issues += [
        f"missing:{name}"
        for name, value in required.items()
        if _text(value) is None
    ]
    if ci.run_attempt is None:
        issues.append("missing:ci.run_attempt")
    elif ci.run_attempt <= 0:
        issues.append("invalid:ci.run_attempt")
    if ci.candidate_sha is not None and not _sha(ci.candidate_sha):
        issues.append("invalid:ci.candidate_sha")
    if (
        _sha(ci.candidate_sha)
        and _sha(binding.head_sha)
        and ci.candidate_sha.lower() != binding.head_sha.lower()
    ):
        issues.append("mismatch:ci.candidate_sha")
    if ci.stale:
        issues.append("stale:ci")
    if (
        _text(ci.source_repository)
        and _text(binding.repo)
        and ci.source_repository != binding.repo
    ):
        issues.append("mismatch:ci.source_repository")
    if _text(ci.artifact_id):
        if _text(ci.producer_job) is None:
            issues.append("missing:ci.producer_job_for_artifact")
        if ci.run_attempt is None:
            issues.append("missing:ci.run_attempt_for_artifact")
    return issues


def _review_evidence_issues(binding: WorkstreamBinding) -> list[str]:
    issues: list[str] = []
    review = binding.review
    if review is None:
        return ["missing:review"]
    if not _sha(review.reviewed_sha):
        issues.append("missing_exact:review.reviewed_sha")
    elif (
        _sha(binding.head_sha)
        and review.reviewed_sha.lower() != binding.head_sha.lower()
    ):
        issues.append("mismatch:review.reviewed_sha")
    if review.stale:
        issues.append("stale:review")
    if (
        _text(review.source_repository)
        and _text(binding.repo)
        and review.source_repository != binding.repo
    ):
        issues.append("mismatch:review.source_repository")
    return issues


def _action_issues(
    binding: WorkstreamBinding,
    resolution: LifecycleResolution,
) -> list[str]:
    action = resolution.action
    if action is None:
        return []
    issues: list[str] = []
    exact_head_actions = {
        NextAction.RUN_EXACT_HEAD_CI,
        NextAction.CLASSIFY_CI,
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.CAPTURE_REVIEW_RESULT,
        NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
        NextAction.CONTINUE_SAME_WRITER,
        NextAction.VERIFY_CANDIDATE_SHA,
        NextAction.INVALIDATE_PRIOR_REVIEW,
        NextAction.START_RE_REVIEW,
        NextAction.REQUEST_PARENT_REVIEW,
        NextAction.RECONCILE_CI_EVIDENCE,
        NextAction.RECONCILE_REVIEW_EVIDENCE,
    }
    if action in exact_head_actions and not _sha(binding.head_sha):
        issues.append("missing_exact:head_sha")

    pr_actions = {
        NextAction.RUN_EXACT_HEAD_CI,
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.CAPTURE_REVIEW_RESULT,
        NextAction.START_RE_REVIEW,
        NextAction.REQUEST_PARENT_REVIEW,
        NextAction.RECONCILE_REVIEW_EVIDENCE,
    }
    if action in pr_actions and binding.pr_number is None:
        issues.append("missing:pr_number")

    ci_evidence_actions = {
        NextAction.CLASSIFY_CI,
        NextAction.RECONCILE_CI_EVIDENCE,
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.START_RE_REVIEW,
    }
    if action in ci_evidence_actions:
        issues.extend(_ci_evidence_issues(binding))

    if action in {
        NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
        NextAction.CONTINUE_SAME_WRITER,
    } and _text(binding.writer_lineage) is None:
        issues.append("missing:writer_lineage")

    if action in {
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.START_RE_REVIEW,
    } and _text(binding.reviewer_lineage) is None:
        issues.append("missing:reviewer_lineage")

    if action is NextAction.CAPTURE_REVIEW_RESULT:
        if _text(binding.reviewer_lineage) is None:
            issues.append("missing:reviewer_lineage")

    if action in {
        NextAction.REQUEST_PARENT_REVIEW,
        NextAction.RECONCILE_REVIEW_EVIDENCE,
        NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
    }:
        issues.extend(_review_evidence_issues(binding))

    return issues


def _context(binding: WorkstreamBinding) -> LifecycleContext:
    authorization = binding.authorization
    provider = binding.provider_source
    return LifecycleContext(
        state=binding.lifecycle_state,
        review_outcome=binding.review.outcome if binding.review else None,
        ci_outcome=binding.ci.outcome if binding.ci else None,
        waiting_class=binding.waiting_class,
        failure_class=binding.error_class,
        resume_state=binding.resume_state,
        review_stale=bool(binding.review and binding.review.stale),
        authorization_decision=authorization.decision if authorization else None,
        authorized_action=authorization.action if authorization else None,
        source_binding_status=provider.status if provider else None,
    )


def _stale_ci(ci: CIBinding, reason: str) -> CIBinding:
    return replace(ci, stale=True, stale_reason=ci.stale_reason or reason)


def _stale_review(review: ReviewBinding, reason: str) -> ReviewBinding:
    return replace(
        review,
        stale=True,
        stale_reason=review.stale_reason or reason,
    )


def _invalidate_sha_move(
    current: WorkstreamBinding,
    previous: Optional[WorkstreamBinding],
):
    if (
        not previous
        or not previous.head_sha
        or not current.head_sha
        or previous.head_sha.lower() == current.head_sha.lower()
    ):
        return current, False, False, False

    reason = (
        f"candidate SHA moved from {previous.head_sha} to {current.head_sha}"
    )
    review = current.review or previous.review
    ci = current.ci or previous.ci
    review_invalidated = False
    ci_invalidated = False

    if review and review.reviewed_sha:
        if review.reviewed_sha.lower() != current.head_sha.lower():
            review = _stale_review(review, reason)
            review_invalidated = True

    if ci and ci.candidate_sha:
        if ci.candidate_sha.lower() != current.head_sha.lower():
            ci = _stale_ci(ci, reason)
            ci_invalidated = True

    return (
        replace(
            current,
            lifecycle_state=LifecycleState.NEW_SHA,
            review=review,
            ci=ci,
        ),
        True,
        review_invalidated,
        ci_invalidated,
    )


def _invalidate_mismatch(binding: WorkstreamBinding):
    review = binding.review
    ci = binding.ci
    review_invalidated = False
    ci_invalidated = False
    state = binding.lifecycle_state

    if (
        review
        and review.reviewed_sha
        and binding.head_sha
        and review.reviewed_sha.lower() != binding.head_sha.lower()
    ):
        review = _stale_review(
            review,
            f"reviewed SHA {review.reviewed_sha} != candidate {binding.head_sha}",
        )
        review_invalidated = True
        if state in {
            LifecycleState.REVIEW_RESULT,
            LifecycleState.PARENT_REVIEW_PENDING,
            LifecycleState.REVIEWER_ACTIVE,
        }:
            state = LifecycleState.PRIOR_REVIEW_STALE

    if (
        ci
        and ci.candidate_sha
        and binding.head_sha
        and ci.candidate_sha.lower() != binding.head_sha.lower()
    ):
        ci = _stale_ci(
            ci,
            f"CI candidate SHA {ci.candidate_sha} != candidate {binding.head_sha}",
        )
        ci_invalidated = True

    return (
        replace(binding, review=review, ci=ci, lifecycle_state=state),
        review_invalidated,
        ci_invalidated,
    )


def _blocked(
    binding: WorkstreamBinding,
    issues: Iterable[str],
    gate: StopGate = StopGate.INCOMPLETE_BINDING,
):
    items = tuple(dict.fromkeys(issues))
    resolution = ensure_lifecycle_resolution(
        binding.lifecycle_state,
        stop_gate=gate,
        reason="; ".join(items),
    )
    bound = replace(binding, next_action=None, stop_gate=gate.value)
    return ReconciliationResult(bound, resolution, items)


def reconcile_workstream(
    binding: WorkstreamBinding,
    previous: Optional[WorkstreamBinding] = None,
) -> ReconciliationResult:
    structural = _structural_issues(binding)
    if structural:
        return _blocked(binding, structural)

    current, moved, invalidated_review_move, invalidated_ci_move = (
        _invalidate_sha_move(binding, previous)
    )
    current, invalidated_review_mismatch, invalidated_ci_mismatch = (
        _invalidate_mismatch(current)
    )
    invalidated_review = (
        invalidated_review_move or invalidated_review_mismatch
    )
    invalidated_ci = invalidated_ci_move or invalidated_ci_mismatch

    if _text(current.action_in_flight):
        resolution = ensure_lifecycle_resolution(
            current.lifecycle_state,
            action=NextAction.VERIFY_ACTION_IN_FLIGHT,
            reason=(
                "reconcile authoritative post-state before any duplicate action"
            ),
        )
    else:
        resolution = resolve_next_action(_context(current))

    action_issues = _action_issues(current, resolution)
    if action_issues:
        gate = (
            resolution.stop_gate
            if resolution.stop_gate is not None
            else StopGate.INCOMPLETE_BINDING
        )
        result = _blocked(current, action_issues, gate)
        return replace(
            result,
            candidate_sha_moved=moved,
            prior_review_invalidated=invalidated_review,
            prior_ci_invalidated=invalidated_ci,
        )

    bound = replace(
        current,
        next_action=resolution.action.value if resolution.action else None,
        stop_gate=(
            resolution.stop_gate.value
            if resolution.stop_gate
            else None
        ),
    )
    return ReconciliationResult(
        bound,
        resolution,
        (),
        moved,
        invalidated_review,
        invalidated_ci,
    )


def _provider_session_key(
    binding: WorkstreamBinding,
) -> Optional[tuple[str, str]]:
    if binding.provider_source is not None:
        session_key = binding.provider_source.session_key
        if session_key is not None:
            return session_key
    legacy_jules_session = _text(binding.jules_session_id)
    if legacy_jules_session is not None:
        return "jules", legacy_jules_session
    return None


def reconcile_portfolio(
    bindings: Iterable[WorkstreamBinding],
    previous_by_lane: Optional[Mapping[LaneKey, WorkstreamBinding]] = None,
) -> tuple[ReconciliationResult, ...]:
    items = tuple(bindings)
    previous_by_lane = previous_by_lane or {}

    lane_keys = [canonical_lane_key(item) for item in items]
    lane_counts = Counter(key for key in lane_keys if key is not None)

    session_lanes: dict[tuple[str, str], set[LaneKey]] = defaultdict(set)
    for item, lane_key in zip(items, lane_keys):
        if lane_key is None or item.provider_source is None:
            continue
        session_key = _provider_session_key(item)
        if session_key is not None:
            session_lanes[session_key].add(lane_key)

    ambiguous_sessions = {
        session_key
        for session_key, lanes in session_lanes.items()
        if len(lanes) > 1
    }

    results: list[ReconciliationResult] = []
    for item, lane_key in zip(items, lane_keys):
        if lane_key is not None and lane_counts[lane_key] > 1:
            results.append(
                _blocked(
                    item,
                    [f"ambiguous:duplicate_lane:{lane_key!r}"],
                    StopGate.AMBIGUOUS_LANE_BINDING,
                )
            )
            continue

        session_key = _provider_session_key(item)
        if session_key in ambiguous_sessions:
            results.append(
                _blocked(
                    item,
                    [f"ambiguous:provider_session_across_lanes:{session_key!r}"],
                    StopGate.AMBIGUOUS_PROVIDER_SESSION,
                )
            )
            continue

        previous = (
            previous_by_lane.get(lane_key)
            if lane_key is not None
            else None
        )
        results.append(reconcile_workstream(item, previous))
    return tuple(results)
