"""Deterministic, side-effect-free replay oracle for sanitized V2 scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


FIXTURE_DIR = Path(__file__).with_name("fixtures")


@dataclass(frozen=True)
class ReplayCase:
    scenario_id: str
    title: str
    kind: str
    domains: tuple[str, ...]
    inputs: Mapping[str, Any]
    expected: Mapping[str, Any]


def load_corpus() -> list[ReplayCase]:
    payload = json.loads((FIXTURE_DIR / "scenarios.json").read_text(encoding="utf-8"))
    return [
        ReplayCase(
            scenario_id=item["id"],
            title=item["title"],
            kind=item["kind"],
            domains=tuple(item["domains"]),
            inputs=item["inputs"],
            expected=item["expected"],
        )
        for item in payload["scenarios"]
    ]


def canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ReferenceOracle:
    """Small semantic oracle used only to lock replay intent and fixture meaning."""

    def evaluate(self, case: ReplayCase) -> dict[str, Any]:
        i = case.inputs
        kind = case.kind

        if kind == "waiting_stale":
            stale = i["waiting_age_seconds"] > i["threshold_seconds"]
            return {
                "status": "INCIDENT" if stale else "WAITING",
                "signal": "WAITING_INPUT_STALE" if stale else "WAITING_INPUT",
                "authority": i["authority"],
                "next_action": "CONTINUE_SAME_SESSION" if stale and i["authority"] == "AUTO_SAFE" else "NONE",
            }

        if kind == "environment_mismatch":
            bounded = bool(i["bounded_workaround_authorized"])
            return {
                "classification": "ENVIRONMENT_MISMATCH",
                "authority": "AUTO_SAFE" if bounded else "PARENT_REQUIRED",
                "next_action": "CONTINUE_SAME_SESSION" if bounded else "STOP_GATE",
            }

        if kind == "binding_mismatch":
            match = i["expected_channel"] == i["observed_channel"]
            return {
                "binding": "PROVEN" if match else "MISMATCH",
                "decision": "CONTINUE" if match else "FAIL_CLOSED",
                "signal": None if match else "SESSION_CHANNEL_BINDING_MISMATCH",
            }

        if kind == "terminal_session":
            terminal = i["normalized_state"] == "FAILED" and not i["continuation_available"]
            return {
                "state": "FAILED",
                "signal": "SESSION_CONTINUATION_UNAVAILABLE" if terminal else "RECOVERABLE_SESSION",
                "recommendation": "NEW_TASK_RECOMMENDED" if terminal else "SAME_SESSION_RECOVERY",
                "auto_create_task": False,
            }

        if kind == "reviewer_mutation":
            mutated = bool(i["reviewer_code_diff"])
            return {
                "review_valid": not mutated,
                "signal": "REVIEWER_MUTATION_DETECTED" if mutated else None,
                "route_findings": False if mutated else True,
            }

        if kind == "pass_without_evidence":
            accepted = bool(i["pass_text"]) and all(
                [i["reviewed_sha_matches"], i["ci_exact_head_clean"], i["artifact_binding_clean"]]
            )
            return {"accepted": accepted, "decision": "PARENT_REVIEW_PENDING" if accepted else "EVIDENCE_INCOMPLETE"}

        if kind == "ambiguous_writer_binding":
            proven = len(i["candidate_writer_sessions"]) == 1
            return {
                "writer_binding": "PROVEN" if proven else "AMBIGUOUS",
                "decision": "ROUTE" if proven else "FAIL_CLOSED",
            }

        if kind == "candidate_sha_moved":
            stale = i["reviewed_sha"] != i["current_candidate_sha"]
            return {
                "prior_review": "STALE" if stale else "CURRENT",
                "next_action": "RE_CI_THEN_REVIEW" if stale else "NONE",
            }

        if kind == "ci_artifact_attempt_mismatch":
            clean = i["expected_run_id"] == i["artifact_run_id"] and i["expected_attempt"] == i["artifact_attempt"]
            return {"binding": "CLEAN" if clean else "MISMATCH", "decision": "ACCEPT" if clean else "REJECT_EVIDENCE"}

        if kind == "provider_failure_matrix":
            normalized = []
            for failure in i["failures"]:
                token = failure["kind"]
                if token in {"401", "403"}:
                    klass = "AUTHORIZATION_FAILURE"
                elif token == "429":
                    klass = "RATE_LIMITED"
                elif token.startswith("5"):
                    klass = "PROVIDER_SERVER_FAILURE"
                elif token == "network":
                    klass = "NETWORK_FAILURE"
                else:
                    klass = "UNKNOWN_FAILURE"
                normalized.append({"kind": token, "class": klass, "blind_retry": False})
            return {"failures": normalized}

        if kind == "unknown_jules_state":
            documented = set(i["documented_states"])
            return {"normalized_state": i["raw_state"] if i["raw_state"] in documented else "UNKNOWN"}

        if kind == "ambiguous_write":
            return {
                "write_outcome": "AMBIGUOUS",
                "blind_retry": False,
                "next_action": "READ_AUTHORITATIVE_POST_STATE",
            }

        if kind == "duplicate_correction":
            duplicate = i["operation_key"] in set(i["existing_operation_keys"])
            return {
                "duplicate": duplicate,
                "decision": "SUPPRESS_DUPLICATE_OPERATION" if duplicate else "RESERVE_OPERATION",
            }

        if kind == "task_budget_uncertain":
            uncertain = i["lifetime_usage_known"] is False
            return {
                "current_enumeration": i["current_enumeration"],
                "lifetime_budget": "UNKNOWN" if uncertain else "KNOWN",
                "new_task_auto_spend": False,
                "decision": "PARENT_REQUIRED" if uncertain else "POLICY_EVALUATE",
            }

        if kind == "independent_lanes":
            executable = [lane["id"] for lane in i["lanes"] if lane["state"] == "EXECUTABLE"]
            blocked = [lane["id"] for lane in i["lanes"] if lane["state"] == "BLOCKED"]
            return {"blocked": blocked, "executable": executable, "freeze_all": False}

        if kind == "forgotten_lane":
            forgotten = not i["next_transition"] and not i["stop_gate"]
            return {"signal": "FORGOTTEN_LANE" if forgotten else None, "cycle_ok": not forgotten}

        if kind == "review_to_same_writer":
            routable = all(
                [
                    i["review_complete"],
                    i["reviewed_sha_matches"],
                    i["writer_binding_proven"],
                    not i["duplicate_operation"],
                ]
            )
            return {
                "route": "SAME_WRITER" if routable else "FAIL_CLOSED",
                "create_new_task": False,
            }

        if kind == "correction_new_sha":
            changed = i["old_sha"] != i["new_sha"]
            return {
                "prior_review": "STALE" if changed else "CURRENT",
                "ci_required": changed,
                "review_required": changed,
            }

        if kind == "autosafe_untreated":
            failed = bool(i["auto_safe_incident_present"] and not i["treated_by_cycle_end"])
            return {"cycle": "CONTROL_CYCLE_FAILED" if failed else "OK"}

        if kind == "activation_missing":
            state = i.get("activation_state")
            return {"activation_state": state if state in {"SHADOW", "CANARY", "ACTIVE_AUTO_SAFE"} else "SHADOW"}

        raise AssertionError(f"Unhandled replay kind: {kind}")
