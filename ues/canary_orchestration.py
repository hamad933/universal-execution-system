from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from .idempotency import EffectIdentity, canonical_request_digest, effect_operation_key
from .operation_records import sanitize_receipt
from .providers.base import ProviderError, WriteOutcomeUnknown
from .routing import WAITING_SAME_SESSION_CONTINUATION
from .state_store import (
    StateStore,
    StateStoreError,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)

SCHEMA_VERSION = "2.0"
WAITING_EFFECT_ACTION = "waiting-answer"


class JulesMessageClient(Protocol):
    def send_message(
        self,
        session: str,
        prompt: str,
        *,
        expected_repository: str | tuple[str, str] | None = None,
        expected_source: str | None = None,
    ) -> dict[str, Any]: ...


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _policy_actions(value: Iterable[str] | None) -> set[str]:
    if value is None:
        return set()
    return {str(item).strip().upper() for item in value if str(item).strip()}


def _result(decision: str, **values: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "live_authority_granted_by_code": False,
        "new_task_or_session_created": False,
        "safe_to_blind_retry": False,
        **values,
    }


def _validate_waiting_effect(effect: EffectIdentity) -> tuple[bool, str | None, dict[str, str]]:
    target = dict(effect.target)
    if effect.action != WAITING_EFFECT_ACTION:
        return False, "WAITING_EFFECT_ACTION_REQUIRED", target
    if target.get("provider", "").casefold() != "jules":
        return False, "JULES_EFFECT_TARGET_REQUIRED", target
    if not target.get("session_id") or not target.get("waiting_activity_id"):
        return False, "EXACT_SESSION_AND_WAITING_ACTIVITY_REQUIRED", target
    return True, None, target


def _terminalize_definitive_provider_failure(
    store: StateStore,
    *,
    lane_id: str,
    operation_key: str,
    lease_id: str,
    evidence: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Persist a definitive provider rejection and release its lane lease.

    This helper is only used after the external provider contract returned a
    definitive error rather than an ambiguous write outcome. It never retries
    the provider operation.
    """

    read = store.read_operation(operation_key)
    if read.status != "OK" or read.record is None:
        return {"state_persisted": False, "lease_released": False, "reason": "OPERATION_STATE_UNAVAILABLE"}
    operation = read.record
    if operation.lane_id != lane_id or operation.state != "IN_FLIGHT":
        return {"state_persisted": False, "lease_released": False, "reason": "OPERATION_STATE_NOT_IN_FLIGHT"}

    operation.state = "REJECTED"
    operation.updated_at = _iso(now)
    operation.reconciliation_required = False
    operation.receipt = sanitize_receipt(
        {
            **operation.receipt,
            "state": "REJECTED",
            "provider_failure": dict(evidence),
        }
    )
    store.compare_and_swap_operation(operation_key, read.version, operation)

    lease_released = False
    try:
        store.release_lease(lane_id, lease_id, now=now)
        lease_released = True
    except StateStoreError:
        # A retained lease is fail-closed and will expire/reconcile; the durable
        # operation is already terminal and cannot be replayed automatically.
        lease_released = False
    return {"state_persisted": True, "lease_released": lease_released, "reason": None}


def execute_jules_waiting_canary(
    *,
    store: StateStore,
    client: JulesMessageClient,
    effect: EffectIdentity,
    prompt: str,
    project_auto_safe_actions: Iterable[str] | None,
    expected_repository: str | tuple[str, str],
    expected_source: str,
    canary_authority_event_id: str,
    observed_start: Mapping[str, Any] | None,
    owner: str,
    ttl_seconds: int = 120,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one *already-authorized* Jules waiting-answer canary boundary.

    The function does not create authority. Project policy must explicitly allow
    WAITING_SAME_SESSION_CONTINUATION and StateStore must independently contain a
    matching, unexpired one-shot CanaryGrant for the exact lane/action/target.
    The durable operation is claimed IN_FLIGHT before ``send_message`` is called.

    Current GS/CEP adapters intentionally have empty action allowlists, so this
    function remains unreachable for those projects without a separate governed
    project/canary authority change.
    """

    current = _utc(now)
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    if not str(expected_source or "").strip():
        return _result("EXACT_JULES_SOURCE_REQUIRED", provider_call_invoked=False)
    if not str(canary_authority_event_id or "").strip():
        return _result("CANARY_AUTHORITY_EVENT_REQUIRED", provider_call_invoked=False)

    actions = _policy_actions(project_auto_safe_actions)
    if WAITING_SAME_SESSION_CONTINUATION not in actions:
        return _result(
            "PROJECT_ACTION_POLICY_DENIED",
            provider_call_invoked=False,
            required_project_action=WAITING_SAME_SESSION_CONTINUATION,
        )

    valid, reason, target = _validate_waiting_effect(effect)
    if not valid:
        return _result(reason or "INVALID_WAITING_EFFECT", provider_call_invoked=False)

    operation_key = effect_operation_key(effect)
    request_digest = canonical_request_digest({"prompt": prompt})
    claim = claim_operation(
        store,
        lane_id=effect.lane_id,
        owner=owner,
        operation_key=operation_key,
        action=effect.action,
        request_digest=request_digest,
        ttl_seconds=ttl_seconds,
        receipt={
            "provider": "JULES",
            "operation": "sendMessage",
            "request_digest": request_digest,
        },
        effect_identity=effect,
        observed_start=observed_start,
        canary_authority_event_id=canary_authority_event_id,
        now=current,
    )
    if claim.get("decision") != "CLAIMED":
        return _result(
            str(claim.get("decision") or "CLAIM_DENIED"),
            provider_call_invoked=False,
            operation_key=operation_key,
            claim=claim,
        )

    lease_id = str(claim.get("lease_id") or "")
    session_id = target["session_id"]
    try:
        provider_receipt = client.send_message(
            session_id,
            prompt,
            expected_repository=expected_repository,
            expected_source=expected_source,
        )
    except WriteOutcomeUnknown as exc:
        try:
            saved = record_unknown_write(
                store,
                lane_id=effect.lane_id,
                operation_key=operation_key,
                result=exc.to_dict(),
                now=current,
            )
        except StateStoreError as state_exc:
            return _result(
                "WRITE_OUTCOME_UNKNOWN_STATE_PERSISTENCE_FAILED",
                provider_call_invoked=True,
                operation_key=operation_key,
                reconciliation_required=True,
                state_error=type(state_exc).__name__,
            )
        return _result(
            "WRITE_OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
            provider_call_invoked=True,
            operation_key=operation_key,
            operation_state=saved.record.state if saved.record else "UNKNOWN",
            reconciliation_required=True,
        )
    except ProviderError as exc:
        try:
            terminal = _terminalize_definitive_provider_failure(
                store,
                lane_id=effect.lane_id,
                operation_key=operation_key,
                lease_id=lease_id,
                evidence=exc.to_dict(),
                now=current,
            )
        except StateStoreError as state_exc:
            return _result(
                "DEFINITIVE_PROVIDER_FAILURE_STATE_PERSISTENCE_FAILED",
                provider_call_invoked=True,
                operation_key=operation_key,
                reconciliation_required=True,
                provider_error=exc.category,
                state_error=type(state_exc).__name__,
            )
        return _result(
            "DEFINITIVE_PROVIDER_FAILURE",
            provider_call_invoked=True,
            operation_key=operation_key,
            provider_error=exc.category,
            operation_state="REJECTED" if terminal["state_persisted"] else "UNKNOWN",
            reconciliation_required=not terminal["state_persisted"],
            lease_released=terminal["lease_released"],
        )
    except Exception as exc:
        # An unexpected exception at the provider boundary is conservatively an
        # unknown write outcome. Never infer that no effect occurred.
        try:
            saved = record_unknown_write(
                store,
                lane_id=effect.lane_id,
                operation_key=operation_key,
                result={"category": type(exc).__name__, "safe_to_blind_retry": False},
                now=current,
            )
        except StateStoreError as state_exc:
            return _result(
                "UNEXPECTED_PROVIDER_OUTCOME_STATE_PERSISTENCE_FAILED",
                provider_call_invoked=True,
                operation_key=operation_key,
                reconciliation_required=True,
                state_error=type(state_exc).__name__,
            )
        return _result(
            "UNEXPECTED_PROVIDER_OUTCOME_RECONCILE_REQUIRED",
            provider_call_invoked=True,
            operation_key=operation_key,
            operation_state=saved.record.state if saved.record else "UNKNOWN",
            reconciliation_required=True,
        )

    try:
        saved = record_authoritative_readback(
            store,
            lane_id=effect.lane_id,
            operation_key=operation_key,
            observed=True,
            evidence={"provider_receipt": sanitize_receipt(provider_receipt)},
            now=current,
        )
    except StateStoreError as exc:
        # JulesClient only returns after authoritative Activities confirmation,
        # so the external effect is known delivered even if durable state update
        # failed. Blocking further execution prevents duplicate delivery.
        return _result(
            "PROVIDER_CONFIRMED_STATE_PERSISTENCE_FAILED",
            provider_call_invoked=True,
            operation_key=operation_key,
            provider_confirmed=True,
            reconciliation_required=True,
            state_error=type(exc).__name__,
        )

    return _result(
        "CONFIRMED",
        provider_call_invoked=True,
        operation_key=operation_key,
        provider_confirmed=True,
        operation_state=saved.record.state if saved.record else "CONFIRMED",
        reconciliation_required=False,
    )
