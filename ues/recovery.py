from __future__ import annotations

from typing import Any


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
