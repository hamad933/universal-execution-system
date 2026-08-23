from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LifecycleState(str, Enum):
    WRITER_ACTIVE = "WRITER_ACTIVE"
    CANDIDATE_PUBLISHED = "CANDIDATE_PUBLISHED"
    CI_RUNNING = "CI_RUNNING"
    CI_CLASSIFIED = "CI_CLASSIFIED"
    REVIEWER_ACTIVE = "REVIEWER_ACTIVE"
    REVIEW_RESULT = "REVIEW_RESULT"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"
    SAME_WRITER_CONTINUATION = "SAME_WRITER_CONTINUATION"
    NEW_SHA = "NEW_SHA"
    PRIOR_REVIEW_STALE = "PRIOR_REVIEW_STALE"
    RE_REVIEW = "RE_REVIEW"
    PARENT_REVIEW_PENDING = "PARENT_REVIEW_PENDING"
    AWAITING_USER_FEEDBACK = "AWAITING_USER_FEEDBACK"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    FORGOTTEN = "FORGOTTEN"


class ReviewOutcome(str, Enum):
    PASS = "PASS"
    FINDINGS = "FINDINGS"


class CIOutcome(str, Enum):
    PASS = "PASS"
    FAILURE = "FAILURE"


class WaitingClass(str, Enum):
    POLICY_RESOLVABLE = "POLICY_RESOLVABLE"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    CI_DEPENDENT = "CI_DEPENDENT"
    REVIEW_DEPENDENT = "REVIEW_DEPENDENT"
    TOOL_LIMIT = "TOOL_LIMIT"
    SHARED_CONTRACT_REQUIRED = "SHARED_CONTRACT_REQUIRED"
    SCOPE_OR_NEW_TASK_REQUIRED = "SCOPE_OR_NEW_TASK_REQUIRED"
    OWNER_DECISION_REQUIRED = "OWNER_DECISION_REQUIRED"
    UNCLASSIFIED = "UNCLASSIFIED"


class FailureClass(str, Enum):
    RECOVERABLE = "RECOVERABLE"
    SESSION_CONTINUATION_UNAVAILABLE = "SESSION_CONTINUATION_UNAVAILABLE"
    CANDIDATE_DEFECT = "CANDIDATE_DEFECT"
    PARENT_REQUIRED = "PARENT_REQUIRED"
    OWNER_REQUIRED = "OWNER_REQUIRED"
    UNCLASSIFIED = "UNCLASSIFIED"


class NextAction(str, Enum):
    PUBLISH_CANDIDATE = "PUBLISH_CANDIDATE"
    RUN_EXACT_HEAD_CI = "RUN_EXACT_HEAD_CI"
    CLASSIFY_CI = "CLASSIFY_CI"
    START_EXACT_SHA_REVIEW = "START_EXACT_SHA_REVIEW"
    CAPTURE_REVIEW_RESULT = "CAPTURE_REVIEW_RESULT"
    ROUTE_FINDINGS_TO_SAME_WRITER = "ROUTE_FINDINGS_TO_SAME_WRITER"
    CONTINUE_SAME_WRITER = "CONTINUE_SAME_WRITER"
    VERIFY_CANDIDATE_SHA = "VERIFY_CANDIDATE_SHA"
    INVALIDATE_PRIOR_REVIEW = "INVALIDATE_PRIOR_REVIEW"
    START_RE_REVIEW = "START_RE_REVIEW"
    REQUEST_PARENT_REVIEW = "REQUEST_PARENT_REVIEW"
    CONTINUE_SAME_SESSION = "CONTINUE_SAME_SESSION"
    RECONCILE_CI_EVIDENCE = "RECONCILE_CI_EVIDENCE"
    RECONCILE_REVIEW_EVIDENCE = "RECONCILE_REVIEW_EVIDENCE"
    RECOVER_SAME_LINEAGE = "RECOVER_SAME_LINEAGE"
    RESUME_PAUSED_LANE = "RESUME_PAUSED_LANE"
    VERIFY_ACTION_IN_FLIGHT = "VERIFY_ACTION_IN_FLIGHT"


class Capability(str, Enum):
    READ_ONLY = "READ_ONLY"
    MUTATION = "MUTATION"


class AuthorizationDecision(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    DENIED = "DENIED"


class SourceBindingStatus(str, Enum):
    PROVEN_EXPLICIT = "PROVEN_EXPLICIT"
    PROPOSED_UNVERIFIED = "PROPOSED_UNVERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    MISMATCH = "MISMATCH"


class StopGate(str, Enum):
    PARENT_AUTHORITY_REQUIRED = "PARENT_AUTHORITY_REQUIRED"
    PARENT_REQUIRED_NEW_TASK = "PARENT_REQUIRED_NEW_TASK"
    OWNER_AUTHORITY_REQUIRED = "OWNER_AUTHORITY_REQUIRED"
    SHARED_CONTRACT_REQUIRED = "SHARED_CONTRACT_REQUIRED"
    UNCLASSIFIED_WAITING = "UNCLASSIFIED_WAITING"
    UNCLASSIFIED_FAILURE = "UNCLASSIFIED_FAILURE"
    PAUSE_REQUIRES_AUTHORITY = "PAUSE_REQUIRES_AUTHORITY"
    CI_FAILURE_CLASSIFICATION_REQUIRED = "CI_FAILURE_CLASSIFICATION_REQUIRED"
    INCOMPLETE_BINDING = "INCOMPLETE_BINDING"
    INVALID_BINDING = "INVALID_BINDING"
    FORGOTTEN_LANE = "FORGOTTEN_LANE"
    AMBIGUOUS_LANE_BINDING = "AMBIGUOUS_LANE_BINDING"
    AMBIGUOUS_PROVIDER_SESSION = "AMBIGUOUS_PROVIDER_SESSION"
    EXTERNAL_AUTHORIZATION_REQUIRED = "EXTERNAL_AUTHORIZATION_REQUIRED"
    EXTERNAL_AUTHORIZATION_DENIED = "EXTERNAL_AUTHORIZATION_DENIED"
    AUTHORIZATION_BINDING_MISMATCH = "AUTHORIZATION_BINDING_MISMATCH"
    PROVIDER_SOURCE_BINDING_REQUIRED = "PROVIDER_SOURCE_BINDING_REQUIRED"
    AMBIGUOUS_PROVIDER_SOURCE_BINDING = "AMBIGUOUS_PROVIDER_SOURCE_BINDING"
    PROVIDER_SOURCE_MISMATCH = "PROVIDER_SOURCE_MISMATCH"


MUTATION_ACTIONS = frozenset(
    {
        NextAction.PUBLISH_CANDIDATE,
        NextAction.RUN_EXACT_HEAD_CI,
        NextAction.START_EXACT_SHA_REVIEW,
        NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
        NextAction.CONTINUE_SAME_WRITER,
        NextAction.START_RE_REVIEW,
        NextAction.REQUEST_PARENT_REVIEW,
        NextAction.CONTINUE_SAME_SESSION,
        NextAction.RECOVER_SAME_LINEAGE,
        NextAction.RESUME_PAUSED_LANE,
    }
)


class ForgottenLaneError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleContext:
    state: LifecycleState
    review_outcome: Optional[ReviewOutcome] = None
    ci_outcome: Optional[CIOutcome] = None
    waiting_class: Optional[WaitingClass] = None
    failure_class: Optional[FailureClass] = None
    resume_state: Optional[LifecycleState] = None
    review_stale: bool = False
    authorization_decision: Optional[AuthorizationDecision] = None
    authorized_action: Optional[NextAction] = None
    source_binding_status: Optional[SourceBindingStatus] = None


@dataclass(frozen=True)
class LifecycleResolution:
    current_state: LifecycleState
    next_state: LifecycleState
    action: Optional[NextAction] = None
    stop_gate: Optional[StopGate] = None
    reason: str = ""
    required_capability: Capability = Capability.READ_ONLY
    required_evidence: tuple[str, ...] = ()

    @property
    def semantic_action(self) -> Optional[NextAction]:
        return self.action

    @property
    def executable(self) -> bool:
        return self.action is not None and self.stop_gate is None


def action_requires_mutation(action: Optional[NextAction]) -> bool:
    return action in MUTATION_ACTIONS


def ensure_lifecycle_resolution(
    state: LifecycleState,
    *,
    next_state: Optional[LifecycleState] = None,
    action: Optional[NextAction] = None,
    stop_gate: Optional[StopGate] = None,
    reason: str = "",
    required_capability: Optional[Capability] = None,
    required_evidence: tuple[str, ...] = (),
) -> LifecycleResolution:
    if action is None and stop_gate is None:
        raise ForgottenLaneError(
            f"{StopGate.FORGOTTEN_LANE.value}: {state.value} requires a semantic next action or Stop Gate"
        )
    capability = required_capability
    if capability is None:
        capability = Capability.MUTATION if action_requires_mutation(action) else Capability.READ_ONLY
    return LifecycleResolution(
        state,
        next_state or state,
        action,
        stop_gate,
        reason,
        capability,
        tuple(dict.fromkeys(required_evidence)),
    )


def _coerce(value: object, enum_type: type[Enum]):
    if value is None:
        return None
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError:
        return None


def _stop(state: LifecycleState, gate: StopGate, reason: str):
    return ensure_lifecycle_resolution(state, stop_gate=gate, reason=reason)


def _transition(
    context: LifecycleContext,
    target: LifecycleState,
    action: NextAction,
    reason: str,
    *,
    required_evidence: tuple[str, ...] = (),
) -> LifecycleResolution:
    capability = Capability.MUTATION if action_requires_mutation(action) else Capability.READ_ONLY
    evidence = list(required_evidence)
    if capability is Capability.READ_ONLY:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            reason=reason,
            required_capability=capability,
            required_evidence=tuple(evidence),
        )

    evidence.extend(("external_authorization", "provider_source_binding:PROVEN_EXPLICIT"))
    source_status = _coerce(context.source_binding_status, SourceBindingStatus)
    if source_status is SourceBindingStatus.AMBIGUOUS:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.AMBIGUOUS_PROVIDER_SOURCE_BINDING,
            reason=f"{reason}; mutation blocked by ambiguous provider/source binding",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )
    if source_status is SourceBindingStatus.MISMATCH:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.PROVIDER_SOURCE_MISMATCH,
            reason=f"{reason}; mutation blocked by mismatched provider/source binding",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )
    if source_status is not SourceBindingStatus.PROVEN_EXPLICIT:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.PROVIDER_SOURCE_BINDING_REQUIRED,
            reason=f"{reason}; explicit provider/source proof is required before mutation",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )

    decision = _coerce(context.authorization_decision, AuthorizationDecision)
    authorized_action = _coerce(context.authorized_action, NextAction)
    if decision is AuthorizationDecision.DENIED and (
        authorized_action is None or authorized_action is action
    ):
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.EXTERNAL_AUTHORIZATION_DENIED,
            reason=f"{reason}; external policy/routing authorization denied mutation",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )
    if decision is AuthorizationDecision.AUTHORIZED and authorized_action is not action:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.AUTHORIZATION_BINDING_MISMATCH,
            reason=f"{reason}; authorization is not bound to the required semantic action",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )
    if decision is not AuthorizationDecision.AUTHORIZED:
        return ensure_lifecycle_resolution(
            context.state,
            next_state=target,
            action=action,
            stop_gate=StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
            reason=f"{reason}; mutation requires an explicit external authorization decision",
            required_capability=capability,
            required_evidence=tuple(evidence),
        )
    return ensure_lifecycle_resolution(
        context.state,
        next_state=target,
        action=action,
        reason=f"{reason}; external authorization and provider/source proof are present",
        required_capability=capability,
        required_evidence=tuple(evidence),
    )


def resolve_next_action(context: LifecycleContext) -> LifecycleResolution:
    state = context.state
    simple = {
        LifecycleState.WRITER_ACTIVE: (
            LifecycleState.CANDIDATE_PUBLISHED,
            NextAction.PUBLISH_CANDIDATE,
        ),
        LifecycleState.CANDIDATE_PUBLISHED: (
            LifecycleState.CI_RUNNING,
            NextAction.RUN_EXACT_HEAD_CI,
        ),
        LifecycleState.CI_RUNNING: (
            LifecycleState.CI_CLASSIFIED,
            NextAction.CLASSIFY_CI,
        ),
        LifecycleState.REVIEWER_ACTIVE: (
            LifecycleState.REVIEW_RESULT,
            NextAction.CAPTURE_REVIEW_RESULT,
        ),
        LifecycleState.CORRECTION_REQUIRED: (
            LifecycleState.SAME_WRITER_CONTINUATION,
            NextAction.CONTINUE_SAME_WRITER,
        ),
        LifecycleState.SAME_WRITER_CONTINUATION: (
            LifecycleState.NEW_SHA,
            NextAction.VERIFY_CANDIDATE_SHA,
        ),
        LifecycleState.NEW_SHA: (
            LifecycleState.PRIOR_REVIEW_STALE,
            NextAction.INVALIDATE_PRIOR_REVIEW,
        ),
        LifecycleState.PRIOR_REVIEW_STALE: (
            LifecycleState.CI_RUNNING,
            NextAction.RUN_EXACT_HEAD_CI,
        ),
        LifecycleState.RE_REVIEW: (
            LifecycleState.REVIEWER_ACTIVE,
            NextAction.START_RE_REVIEW,
        ),
    }
    if state in simple:
        target, action = simple[state]
        return _transition(
            context,
            target,
            action,
            f"{state.value} has deterministic semantic transition to {target.value}",
        )

    if state is LifecycleState.CI_CLASSIFIED:
        outcome = _coerce(context.ci_outcome, CIOutcome)
        failure = _coerce(context.failure_class, FailureClass)
        if outcome is CIOutcome.PASS:
            target = (
                LifecycleState.RE_REVIEW
                if context.review_stale
                else LifecycleState.REVIEWER_ACTIVE
            )
            action = (
                NextAction.START_RE_REVIEW
                if context.review_stale
                else NextAction.START_EXACT_SHA_REVIEW
            )
            return _transition(
                context,
                target,
                action,
                "passing exact-head CI semantically requires exact-SHA review",
                required_evidence=("exact_head_ci_evidence",),
            )
        if outcome is CIOutcome.FAILURE and failure is FailureClass.CANDIDATE_DEFECT:
            return _transition(
                context,
                LifecycleState.CORRECTION_REQUIRED,
                NextAction.CONTINUE_SAME_WRITER,
                "candidate defect semantically returns to same writer",
                required_evidence=("classified_ci_failure",),
            )
        if outcome is CIOutcome.FAILURE and failure is FailureClass.RECOVERABLE:
            return _transition(
                context,
                LifecycleState.CI_RUNNING,
                NextAction.RUN_EXACT_HEAD_CI,
                "recoverable CI failure stays in same semantic lineage",
                required_evidence=("classified_ci_failure",),
            )
        if outcome is CIOutcome.FAILURE and failure is FailureClass.OWNER_REQUIRED:
            return _stop(
                state,
                StopGate.OWNER_AUTHORITY_REQUIRED,
                "CI failure requires Owner authority",
            )
        if outcome is CIOutcome.FAILURE and failure in {
            FailureClass.PARENT_REQUIRED,
            FailureClass.SESSION_CONTINUATION_UNAVAILABLE,
        }:
            return _stop(
                state,
                StopGate.PARENT_AUTHORITY_REQUIRED,
                "CI failure exceeds Domain A authority",
            )
        return _stop(
            state,
            StopGate.CI_FAILURE_CLASSIFICATION_REQUIRED,
            "CI outcome/failure class is incomplete or unknown",
        )

    if state is LifecycleState.REVIEW_RESULT:
        outcome = _coerce(context.review_outcome, ReviewOutcome)
        if outcome is ReviewOutcome.PASS:
            return _transition(
                context,
                LifecycleState.PARENT_REVIEW_PENDING,
                NextAction.REQUEST_PARENT_REVIEW,
                "PASS is not acceptance; Parent review remains",
                required_evidence=("exact_sha_review_evidence",),
            )
        if outcome is ReviewOutcome.FINDINGS:
            return _transition(
                context,
                LifecycleState.CORRECTION_REQUIRED,
                NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
                "findings semantically route to same writer lineage",
                required_evidence=("exact_sha_review_evidence",),
            )
        return _stop(
            state,
            StopGate.INVALID_BINDING,
            "review result must be PASS or FINDINGS",
        )

    if state is LifecycleState.PARENT_REVIEW_PENDING:
        return _stop(
            state,
            StopGate.PARENT_AUTHORITY_REQUIRED,
            "Parent adjudication is outside Domain A",
        )

    if state is LifecycleState.AWAITING_USER_FEEDBACK:
        waiting = _coerce(context.waiting_class, WaitingClass)
        if waiting in {
            WaitingClass.POLICY_RESOLVABLE,
            WaitingClass.ENVIRONMENT_MISMATCH,
            WaitingClass.TOOL_LIMIT,
        }:
            if context.resume_state is None:
                return _stop(
                    state,
                    StopGate.INCOMPLETE_BINDING,
                    "waiting transition requires a proven semantic resume state",
                )
            return _transition(
                context,
                context.resume_state,
                NextAction.CONTINUE_SAME_SESSION,
                f"{waiting.value} has a same-session semantic continuation candidate",
                required_evidence=("current_provider_state", "latest_relevant_activity"),
            )
        if waiting is WaitingClass.CI_DEPENDENT:
            return _transition(
                context,
                LifecycleState.CI_CLASSIFIED,
                NextAction.RECONCILE_CI_EVIDENCE,
                "CI-dependent waiting resolves from exact CI evidence",
                required_evidence=("exact_head_ci_evidence",),
            )
        if waiting is WaitingClass.REVIEW_DEPENDENT:
            return _transition(
                context,
                LifecycleState.REVIEW_RESULT,
                NextAction.RECONCILE_REVIEW_EVIDENCE,
                "review-dependent waiting resolves from exact review evidence",
                required_evidence=("exact_sha_review_evidence",),
            )
        if waiting is WaitingClass.SHARED_CONTRACT_REQUIRED:
            return _stop(
                state,
                StopGate.SHARED_CONTRACT_REQUIRED,
                "shared contract needs integration authority",
            )
        if waiting is WaitingClass.SCOPE_OR_NEW_TASK_REQUIRED:
            return _stop(
                state,
                StopGate.PARENT_REQUIRED_NEW_TASK,
                "new scope/task needs Parent budget authority",
            )
        if waiting is WaitingClass.OWNER_DECISION_REQUIRED:
            return _stop(
                state,
                StopGate.OWNER_AUTHORITY_REQUIRED,
                "Owner decision required",
            )
        return _stop(
            state,
            StopGate.UNCLASSIFIED_WAITING,
            "unclassified waiting fails closed",
        )

    if state is LifecycleState.FAILED:
        failure = _coerce(context.failure_class, FailureClass)
        if failure is FailureClass.RECOVERABLE and context.resume_state is not None:
            return _transition(
                context,
                context.resume_state,
                NextAction.RECOVER_SAME_LINEAGE,
                "recoverable failure has same-lineage semantic recovery candidate",
                required_evidence=("classified_failure",),
            )
        if failure is FailureClass.CANDIDATE_DEFECT:
            return _transition(
                context,
                LifecycleState.CORRECTION_REQUIRED,
                NextAction.CONTINUE_SAME_WRITER,
                "candidate defect semantically returns to same writer",
                required_evidence=("classified_failure",),
            )
        if failure is FailureClass.SESSION_CONTINUATION_UNAVAILABLE:
            return _stop(
                state,
                StopGate.PARENT_REQUIRED_NEW_TASK,
                "terminal session may recommend but cannot spend task budget",
            )
        if failure is FailureClass.PARENT_REQUIRED:
            return _stop(
                state,
                StopGate.PARENT_AUTHORITY_REQUIRED,
                "Parent authority required",
            )
        if failure is FailureClass.OWNER_REQUIRED:
            return _stop(
                state,
                StopGate.OWNER_AUTHORITY_REQUIRED,
                "Owner authority required",
            )
        if failure is FailureClass.RECOVERABLE:
            return _stop(
                state,
                StopGate.INCOMPLETE_BINDING,
                "recoverable failure needs a proven semantic resume state",
            )
        return _stop(
            state,
            StopGate.UNCLASSIFIED_FAILURE,
            "failed state must be classified before action",
        )

    if state is LifecycleState.PAUSED:
        if context.resume_state is not None:
            return _transition(
                context,
                context.resume_state,
                NextAction.RESUME_PAUSED_LANE,
                "paused lane has explicit semantic resume candidate",
                required_evidence=("current_provider_state",),
            )
        return _stop(
            state,
            StopGate.PAUSE_REQUIRES_AUTHORITY,
            "pause without proven resume state fails closed",
        )

    return _stop(
        state,
        StopGate.FORGOTTEN_LANE,
        "forgotten or unknown lifecycle state is invalid",
    )
