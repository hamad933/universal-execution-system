from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Iterable, Mapping, Optional

from .lifecycle import (
    ActionCapability,
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
class ActorBinding:
    role: str
    provider: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    lineage: Optional[str] = None
    source_repository: Optional[str] = None
    source_identity: Optional[str] = None
    proof_status: SourceBindingStatus = SourceBindingStatus.PROPOSED_UNVERIFIED
    evidence_id: Optional[str] = None

    @property
    def status(self) -> SourceBindingStatus:
        return self.proof_status

    @property
    def session_key(self) -> Optional[tuple[str, str]]:
        provider = _text(self.provider)
        session_id = _text(self.session_id)
        if provider is None or session_id is None:
            return None
        return provider.casefold(), session_id


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
    lineage: Optional[str] = None

    @property
    def session_key(self) -> Optional[tuple[str, str]]:
        provider = _text(self.provider)
        session_id = _text(self.session_id)
        if provider is None or session_id is None:
            return None
        return provider.casefold(), session_id

    def as_actor(self, *, fallback_role: Optional[str] = None) -> ActorBinding:
        return ActorBinding(
            role=_text(self.role) or _text(fallback_role) or "UNSPECIFIED",
            provider=self.provider,
            session_id=self.session_id,
            task_id=self.task_id,
            lineage=self.lineage,
            source_repository=self.source_repository,
            source_identity=self.source_identity,
            proof_status=self.status,
            evidence_id=self.evidence_id,
        )


@dataclass(frozen=True)
class ActorBindingResolution:
    role: str
    state: str
    binding: Optional[ActorBinding] = None
    issues: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return (
            self.state == SourceBindingStatus.PROVEN_EXPLICIT.value
            and self.binding is not None
        )


@dataclass(frozen=True)
class EvidenceRequirement:
    name: str
    proven: bool
    current: bool = True
    evidence_id: Optional[str] = None
    actions: tuple[str, ...] = ()

    def applies_to(self, action: Optional[NextAction]) -> bool:
        if not self.actions:
            return True
        if action is None:
            return False
        return action.value in self.actions


@dataclass(frozen=True)
class RequiredEvidenceProfile:
    profile_id: str
    requirements: tuple[EvidenceRequirement, ...] = ()

    def issues_for(self, action: Optional[NextAction]) -> tuple[str, ...]:
        issues: list[str] = []
        seen: set[str] = set()
        for requirement in self.requirements:
            if not requirement.applies_to(action):
                continue
            name = _text(requirement.name)
            if name is None:
                issues.append("invalid:evidence_profile.requirement_name")
                continue
            if name in seen:
                issues.append(f"duplicate:evidence_profile:{name}")
            seen.add(name)
            if not requirement.proven:
                issues.append(f"missing_required_evidence:{name}")
            elif not requirement.current:
                issues.append(f"stale_required_evidence:{name}")
        return tuple(dict.fromkeys(issues))


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
    actor_bindings: tuple[ActorBinding, ...] = ()
    scope_identity: Optional[str] = None
    evidence_profile: Optional[RequiredEvidenceProfile] = None
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_bindings", tuple(self.actor_bindings or ()))

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
    def semantic_candidate(self) -> bool:
        return (
            self.resolution.semantic_candidate
            and not self.issues
            and self.binding.action_in_flight is None
        )

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


def _canonical_actors(binding: WorkstreamBinding) -> tuple[ActorBinding, ...]:
    actors = list(binding.actor_bindings)
    if binding.provider_source is not None:
        actors.append(binding.provider_source.as_actor(fallback_role=binding.role))
    if not actors and _text(binding.jules_session_id):
        actors.append(
            ActorBinding(
                role=_text(binding.role) or "UNSPECIFIED",
                provider="jules",
                session_id=_text(binding.jules_session_id),
                task_id=_text(binding.jules_task_id),
                lineage=(
                    _text(binding.writer_lineage)
                    if str(binding.role or "").upper() == "WRITER"
                    else _text(binding.reviewer_lineage)
                ),
                source_repository=_text(binding.repo),
                proof_status=SourceBindingStatus.PROPOSED_UNVERIFIED,
                evidence_id=None,
            )
        )
    return tuple(actors)


def resolve_actor_binding(
    binding: WorkstreamBinding,
    role: str,
) -> ActorBindingResolution:
    wanted = str(role or "").strip().upper()
    if not wanted:
        return ActorBindingResolution(
            role=wanted,
            state="MISSING",
            issues=("missing:actor_role",),
        )
    candidates = [
        actor
        for actor in _canonical_actors(binding)
        if str(actor.role or "").strip().upper() == wanted
    ]
    if not candidates:
        return ActorBindingResolution(
            role=wanted,
            state="MISSING",
            issues=(f"missing:actor_binding:{wanted}",),
        )

    unique = {}
    for actor in candidates:
        key = (
            _text(actor.provider),
            _text(actor.session_id),
            _text(actor.task_id),
            _text(actor.lineage),
            _text(actor.source_repository),
            _text(actor.source_identity),
            str(actor.role or "").upper(),
            actor.proof_status,
            _text(actor.evidence_id),
        )
        unique[key] = actor
    if len(unique) != 1:
        return ActorBindingResolution(
            role=wanted,
            state=SourceBindingStatus.AMBIGUOUS.value,
            issues=(f"ambiguous:actor_binding:{wanted}",),
        )

    actor = next(iter(unique.values()))
    if _text(actor.source_repository) and _text(binding.repo):
        if actor.source_repository != binding.repo:
            return ActorBindingResolution(
                role=wanted,
                state=SourceBindingStatus.MISMATCH.value,
                binding=actor,
                issues=(f"mismatch:actor_source_repository:{wanted}",),
            )

    if actor.proof_status is SourceBindingStatus.PROVEN_EXPLICIT:
        required = {
            "provider": actor.provider,
            "session_id": actor.session_id,
            "source_repository": actor.source_repository,
            "source_identity": actor.source_identity,
            "evidence_id": actor.evidence_id,
        }
        missing = [
            f"missing:actor_binding:{wanted}:{name}"
            for name, value in required.items()
            if _text(value) is None
        ]
        if missing:
            return ActorBindingResolution(
                role=wanted,
                state=SourceBindingStatus.PROPOSED_UNVERIFIED.value,
                binding=actor,
                issues=tuple(missing),
            )

    return ActorBindingResolution(
        role=wanted,
        state=actor.proof_status.value,
        binding=actor,
    )


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

    if binding.evidence_profile is not None:
        if _text(binding.evidence_profile.profile_id) is None:
            issues.append("missing:evidence_profile.profile_id")
        names = [
            _text(item.name)
            for item in binding.evidence_profile.requirements
            if _text(item.name)
        ]
        if len(names) != len(set(names)):
            issues.append("duplicate:evidence_profile.requirement_name")

    if binding.ci:
        if binding.ci.candidate_sha is not None and not _sha(binding.ci.candidate_sha):
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
        if binding.review.reviewed_sha is not None and not _sha(binding.review.reviewed_sha):
            issues.append("invalid:review.reviewed_sha")
        if (
            _text(binding.review.source_repository)
            and _text(binding.repo)
            and binding.review.source_repository != binding.repo
        ):
            issues.append("mismatch:review.source_repository")

    for actor in _canonical_actors(binding):
        role = str(actor.role or "").strip().upper() or "UNSPECIFIED"
        if _text(actor.provider) is None:
            issues.append(f"missing:actor_binding:{role}:provider")
        if actor.proof_status is SourceBindingStatus.PROVEN_EXPLICIT:
            if _text(actor.session_id) is None:
                issues.append(f"missing:actor_binding:{role}:session_id")
            if _text(actor.source_repository) is None:
                issues.append(f"missing:actor_binding:{role}:source_repository")
            if _text(actor.source_identity) is None:
                issues.append(f"missing:actor_binding:{role}:source_identity")
            if _text(actor.evidence_id) is None:
                issues.append(f"missing:actor_binding:{role}:evidence_id")
        if (
            _text(actor.source_repository)
            and _text(binding.repo)
            and actor.source_repository != binding.repo
        ):
            issues.append(f"mismatch:actor_source_repository:{role}")

    if binding.authorization:
        if _text(binding.authorization.source) is None:
            issues.append("missing:authorization.source")
        if _text(binding.authorization.decision_id) is None:
            issues.append("missing:authorization.decision_id")

    if _text(binding.action_in_flight):
        if _text(binding.lease_id) is None:
            issues.append("missing:lease_id_for_action_in_flight")
        if _text(binding.operation_key) is None:
            issues.append("missing:operation_key_for_action_in_flight")
    return list(dict.fromkeys(issues))


def _ci_evidence_issues(binding: WorkstreamBinding) -> list[str]:
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
    issues = [
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
    return list(dict.fromkeys(issues))


def _review_evidence_issues(binding: WorkstreamBinding) -> list[str]:
    review = binding.review
    if review is None:
        return ["missing:review"]
    issues: list[str] = []
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


def _provider_actor_role(
    binding: WorkstreamBinding,
    action: Optional[NextAction],
) -> Optional[str]:
    if action in {
        NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
        NextAction.CONTINUE_SAME_WRITER,
    }:
        return "WRITER"
    if action in {
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.START_RE_REVIEW,
    }:
        return "REVIEWER"
    if action in {
        NextAction.CONTINUE_SAME_SESSION,
        NextAction.RECOVER_SAME_LINEAGE,
        NextAction.RESUME_PAUSED_LANE,
    }:
        return str(binding.role or "").strip().upper() or None
    return None


def _actor_issues(
    binding: WorkstreamBinding,
    resolution: LifecycleResolution,
) -> list[str]:
    if resolution.required_capability is not ActionCapability.EXTERNAL_EFFECT:
        return []
    role = _provider_actor_role(binding, resolution.action)
    if role is None:
        return []
    actor = resolve_actor_binding(binding, role)
    if actor.proven:
        return []
    if actor.state == SourceBindingStatus.AMBIGUOUS.value:
        return [f"ambiguous:actor_binding:{role}"]
    if actor.state == SourceBindingStatus.MISMATCH.value:
        return [f"mismatch:actor_binding:{role}"]
    return [f"unproven:actor_binding:{role}", *actor.issues]


def _profile_issues(
    binding: WorkstreamBinding,
    action: Optional[NextAction],
) -> list[str]:
    profile = binding.evidence_profile
    if profile is None:
        return []
    return list(profile.issues_for(action))


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

    if action in {
        NextAction.CLASSIFY_CI,
        NextAction.RECONCILE_CI_EVIDENCE,
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.START_RE_REVIEW,
    }:
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

    issues.extend(_actor_issues(binding, resolution))
    issues.extend(_profile_issues(binding, action))
    return list(dict.fromkeys(issues))


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


def _stale_ci(ci: CIBinding, reason: str) -> CIBinding:
    return replace(ci, stale=True, stale_reason=ci.stale_reason or reason)


def _stale_review(review: ReviewBinding, reason: str) -> ReviewBinding:
    return replace(review, stale=True, stale_reason=review.stale_reason or reason)


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

    reason = f"candidate SHA moved from {previous.head_sha} to {current.head_sha}"
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


def _actor_source_map(binding: WorkstreamBinding) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for role in {str(actor.role or "").upper() for actor in _canonical_actors(binding)}:
        if not role:
            continue
        resolution = resolve_actor_binding(binding, role)
        actor = resolution.binding
        if actor is None:
            continue
        result[role] = (
            _text(actor.provider) or "",
            _text(actor.source_repository) or "",
            _text(actor.source_identity) or "",
        )
    return result


def _drift_issues(
    current: WorkstreamBinding,
    previous: Optional[WorkstreamBinding],
) -> list[str]:
    if previous is None:
        return []
    issues: list[str] = []
    comparisons = {
        "repository": (_text(previous.repo), _text(current.repo)),
        "branch": (_text(previous.branch), _text(current.branch)),
        "base_ref": (_text(previous.base_ref), _text(current.base_ref)),
        "baseline_sha": (previous.baseline_sha, current.baseline_sha),
        "scope_identity": (
            _text(previous.scope_identity),
            _text(current.scope_identity),
        ),
        "evidence_profile": (
            _text(previous.evidence_profile.profile_id)
            if previous.evidence_profile
            else None,
            _text(current.evidence_profile.profile_id)
            if current.evidence_profile
            else None,
        ),
    }
    for name, (before, after) in comparisons.items():
        if before != after:
            issues.append(f"drift:{name}:{before!r}->{after!r}")

    previous_sources = _actor_source_map(previous)
    current_sources = _actor_source_map(current)
    for role in sorted(set(previous_sources) | set(current_sources)):
        before = previous_sources.get(role)
        after = current_sources.get(role)
        if before != after:
            issues.append(f"drift:actor_source:{role}:{before!r}->{after!r}")
    return issues


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

    drift = _drift_issues(binding, previous)
    if drift:
        return _blocked(
            binding,
            drift,
            StopGate.BINDING_DRIFT_RECONCILIATION_REQUIRED,
        )

    current, moved, invalidated_review_move, invalidated_ci_move = (
        _invalidate_sha_move(binding, previous)
    )
    current, invalidated_review_mismatch, invalidated_ci_mismatch = (
        _invalidate_mismatch(current)
    )
    invalidated_review = invalidated_review_move or invalidated_review_mismatch
    invalidated_ci = invalidated_ci_move or invalidated_ci_mismatch

    if _text(current.action_in_flight):
        resolution = ensure_lifecycle_resolution(
            current.lifecycle_state,
            action=NextAction.VERIFY_ACTION_IN_FLIGHT,
            reason="reconcile authoritative post-state before any duplicate action",
        )
    else:
        resolution = resolve_next_action(_context(current))

    action_issues = _action_issues(current, resolution)
    if action_issues:
        if any(
            item.startswith(("missing_required_evidence:", "stale_required_evidence:"))
            for item in action_issues
        ):
            gate = StopGate.EVIDENCE_INCOMPLETE
        elif any("actor_binding" in item for item in action_issues):
            gate = (
                StopGate.AMBIGUOUS_ACTOR_BINDING
                if any(item.startswith("ambiguous:") for item in action_issues)
                else StopGate.ACTOR_BINDING_REQUIRED
            )
        else:
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
        stop_gate=resolution.stop_gate.value if resolution.stop_gate else None,
    )
    return ReconciliationResult(
        bound,
        resolution,
        (),
        moved,
        invalidated_review,
        invalidated_ci,
    )


def _session_role_bindings(
    binding: WorkstreamBinding,
) -> tuple[tuple[tuple[str, str], str], ...]:
    result: list[tuple[tuple[str, str], str]] = []
    for actor in _canonical_actors(binding):
        session_key = actor.session_key
        role = str(actor.role or "").strip().upper() or "UNSPECIFIED"
        if session_key is not None:
            result.append((session_key, role))
    return tuple(result)


def reconcile_portfolio(
    bindings: Iterable[WorkstreamBinding],
    previous_by_lane: Optional[Mapping[LaneKey, WorkstreamBinding]] = None,
) -> tuple[ReconciliationResult, ...]:
    items = tuple(bindings)
    previous_by_lane = previous_by_lane or {}

    lane_keys = [canonical_lane_key(item) for item in items]
    lane_counts = Counter(key for key in lane_keys if key is not None)

    session_owners = defaultdict(set)
    for item, lane_key in zip(items, lane_keys):
        if lane_key is None:
            continue
        for session_key, role in _session_role_bindings(item):
            session_owners[session_key].add((lane_key, role))

    ambiguous_sessions = {
        session_key
        for session_key, owners in session_owners.items()
        if len(owners) > 1
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

        conflicts = [
            session_key
            for session_key, _role in _session_role_bindings(item)
            if session_key in ambiguous_sessions
        ]
        if conflicts:
            results.append(
                _blocked(
                    item,
                    [
                        f"ambiguous:provider_session_across_lane_or_role:{key!r}"
                        for key in sorted(set(conflicts))
                    ],
                    StopGate.AMBIGUOUS_PROVIDER_SESSION,
                )
            )
            continue

        previous = previous_by_lane.get(lane_key) if lane_key is not None else None
        results.append(reconcile_workstream(item, previous))
    return tuple(results)
