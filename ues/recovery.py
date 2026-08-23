from __future__ import annotations

from typing import Any, Mapping


def reconcile_checkpoint(checkpoint: dict[str, Any], live_head_sha: str) -> dict[str, Any]:
    confirmed = str(checkpoint.get("confirmed_head_sha") or "")
    expected_post = checkpoint.get("expected_post_sha")
    write_outcome = str(checkpoint.get("write_outcome") or "NONE")

    if not confirmed:
        raise ValueError("checkpoint missing confirmed_head_sha")

    if write_outcome == "UNKNOWN":
        if expected_post and live_head_sha == expected_post:
            verdict = "WRITE_CONFIRMED_BY_POST_STATE"
            next_action = "CONTINUE_FROM_CONFIRMED_POST_STATE"
        elif live_head_sha == confirmed:
            verdict = "INTENDED_REMOTE_WRITE_NOT_OBSERVED"
            next_action = "VERIFY_OPERATION_SPECIFIC_POST_STATE_BEFORE_RETRY"
        else:
            verdict = "DIVERGED_DURING_UNKNOWN_WRITE"
            next_action = "STOP_AND_RECONCILE_LIVE_STATE"
    elif live_head_sha == confirmed:
        verdict = "CHECKPOINT_MATCH"
        next_action = "CONTINUE"
    else:
        verdict = "HEAD_MOVED"
        next_action = "STOP_AND_REBASELINE_OR_RECONCILE"

    return {
        "schema_version": "0.3",
        "verdict": verdict,
        "next_action": next_action,
        "write_outcome": write_outcome,
        "checkpoint_head_sha": confirmed,
        "expected_post_sha": expected_post,
        "live_head_sha": live_head_sha,
        "safe_to_blind_retry": False,
    }


def reconcile_provider_write(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile an attempted provider mutation from authoritative post-state.

    This function never authorizes an automatic retry. It only proves delivery,
    blocks on incomplete/unknown evidence, or exposes a retry *consideration*
    point for a higher-level policy after authoritative reads completed.
    """

    read_complete = bool(observation.get("authoritative_read_complete"))
    state = str(observation.get("post_session_state") or "UNKNOWN").upper()
    pre_ids = {str(value) for value in observation.get("pre_activity_ids") or []}
    activities = observation.get("post_activities") or []
    expected_message = observation.get("expected_user_message")

    if not read_complete:
        return _provider_recovery_result(
            "AUTHORITATIVE_READ_INCOMPLETE",
            state,
            retry_consideration="BLOCKED_UNTIL_AUTHORITATIVE_READ",
        )
    if state == "UNKNOWN":
        return _provider_recovery_result(
            "UNKNOWN_PROVIDER_STATE",
            state,
            retry_consideration="BLOCKED_UNKNOWN_PROVIDER_STATE",
        )
    if not isinstance(activities, list):
        return _provider_recovery_result(
            "AUTHORITATIVE_ACTIVITY_EVIDENCE_INVALID",
            state,
            retry_consideration="BLOCKED_UNTIL_AUTHORITATIVE_READ",
        )

    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        identity = str(activity.get("name") or activity.get("id") or "")
        user_message = activity.get("userMessaged")
        if identity in pre_ids or not isinstance(user_message, Mapping):
            continue
        if expected_message is not None and user_message.get("userMessage") == expected_message:
            return {
                **_provider_recovery_result(
                    "WRITE_CONFIRMED_BY_ACTIVITY",
                    state,
                    retry_consideration="NOT_REQUIRED",
                ),
                "matched_activity": identity or None,
            }

    if state in {"FAILED", "COMPLETED"}:
        return _provider_recovery_result(
            "SESSION_CONTINUATION_UNAVAILABLE",
            state,
            retry_consideration="NEW_TASK_RECOMMENDED_PARENT_ONLY",
        )

    return _provider_recovery_result(
        "WRITE_NOT_OBSERVED_AFTER_AUTHORITATIVE_READ",
        state,
        retry_consideration="POLICY_OR_PARENT_DECISION_REQUIRED",
    )


def _provider_recovery_result(verdict: str, state: str, *, retry_consideration: str) -> dict[str, Any]:
    return {
        "schema_version": "0.4",
        "verdict": verdict,
        "post_session_state": state,
        "retry_consideration": retry_consideration,
        "safe_to_blind_retry": False,
    }
