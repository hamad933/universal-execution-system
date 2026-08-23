"""Deterministic, side-effect-free reference oracle for sanitized V2 replay cases."""
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
    items = []
    for path in sorted(FIXTURE_DIR.glob("scenarios*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items.extend(payload["scenarios"])
    return sorted(
        [ReplayCase(item["id"], item["title"], item["kind"], tuple(item["domains"]), item["inputs"], item["expected"]) for item in items],
        key=lambda case: case.scenario_id,
    )


def canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ReferenceOracle:
    """Independent expectation source. It never imports production ``ues`` modules."""

    def evaluate(self, case: ReplayCase) -> dict[str, Any]:
        i, kind = case.inputs, case.kind
        if kind == "waiting_stale":
            stale = i["waiting_age_seconds"] > i["threshold_seconds"]
            return {"status":"INCIDENT" if stale else "WAITING","signal":"WAITING_INPUT_STALE" if stale else "WAITING_INPUT","authority":i["authority"],"next_action":"CONTINUE_SAME_SESSION" if stale and i["authority"]=="AUTO_SAFE" else "NONE"}
        if kind == "environment_mismatch":
            allowed = bool(i["bounded_workaround_authorized"] and i.get("project_policy_permits") and i.get("exact_state_read") and i.get("latest_activity_read"))
            return {"classification":"ENVIRONMENT_MISMATCH","authority":"AUTO_SAFE" if allowed else "PARENT_REQUIRED","next_action":"CONTINUE_SAME_SESSION" if allowed else "STOP_GATE"}
        if kind == "binding_mismatch":
            match = i["expected_channel"] == i["observed_channel"]
            return {"binding":"PROVEN" if match else "MISMATCH","decision":"CONTINUE" if match else "FAIL_CLOSED","signal":None if match else "SESSION_CHANNEL_BINDING_MISMATCH"}
        if kind == "terminal_session":
            terminal = i["normalized_state"] == "FAILED" and not i["continuation_available"]
            return {"state":"FAILED","signal":"SESSION_CONTINUATION_UNAVAILABLE" if terminal else "RECOVERABLE_SESSION","recommendation":"NEW_TASK_RECOMMENDED" if terminal else "SAME_SESSION_RECOVERY","auto_create_task":False}
        if kind == "reviewer_mutation":
            mutated = bool(i["reviewer_code_diff"])
            return {"review_valid":not mutated,"signal":"REVIEWER_MUTATION_DETECTED" if mutated else None,"route_findings":not mutated}
        if kind == "pass_without_evidence":
            accepted = bool(i["pass_text"] and i["reviewed_sha_matches"] and i["ci_exact_head_clean"] and i["artifact_binding_clean"])
            return {"accepted":accepted,"decision":"PARENT_REVIEW_PENDING" if accepted else "EVIDENCE_INCOMPLETE"}
        if kind == "ambiguous_writer_binding":
            candidates = list(i["candidate_writer_sessions"])
            explicit = bool(i.get("explicit_source_binding"))
            if len(candidates) > 1:
                status, decision = "AMBIGUOUS", "FAIL_CLOSED"
            elif len(candidates) == 1 and explicit:
                status, decision = "PROVEN", "ROUTE"
            elif len(candidates) == 1:
                status, decision = "PROPOSED_UNVERIFIED", "FAIL_CLOSED"
            else:
                status, decision = "UNBOUND", "FAIL_CLOSED"
            return {"writer_binding":status,"decision":decision}
        if kind == "candidate_sha_moved":
            stale = i["reviewed_sha"] != i["current_candidate_sha"]
            return {"prior_review":"STALE" if stale else "CURRENT","next_action":"RE_CI_THEN_REVIEW" if stale else "NONE"}
        if kind == "ci_artifact_attempt_mismatch":
            clean = i["expected_run_id"] == i["artifact_run_id"] and i["expected_attempt"] == i["artifact_attempt"]
            return {"binding":"CLEAN" if clean else "MISMATCH","decision":"ACCEPT" if clean else "REJECT_EVIDENCE"}
        if kind == "provider_failure_matrix":
            out=[]
            for f in i["failures"]:
                token=f["kind"]
                klass="AUTHORIZATION_FAILURE" if token in {"401","403"} else "RATE_LIMITED" if token=="429" else "PROVIDER_SERVER_FAILURE" if token.startswith("5") else "NETWORK_FAILURE" if token=="network" else "UNKNOWN_FAILURE"
                out.append({"kind":token,"class":klass,"blind_retry":False})
            return {"failures":out}
        if kind == "unknown_jules_state":
            return {"normalized_state":i["raw_state"] if i["raw_state"] in set(i["documented_states"]) else "UNKNOWN"}
        if kind == "ambiguous_write":
            return {"write_outcome":"AMBIGUOUS","blind_retry":False,"next_action":"READ_AUTHORITATIVE_POST_STATE"}
        if kind == "duplicate_correction":
            duplicate=i["operation_key"] in set(i["existing_operation_keys"])
            return {"duplicate":duplicate,"decision":"SUPPRESS_DUPLICATE_OPERATION" if duplicate else "RESERVE_OPERATION"}
        if kind == "task_budget_uncertain":
            uncertain=i["lifetime_usage_known"] is False
            return {"current_enumeration":i["current_enumeration"],"lifetime_budget":"UNKNOWN" if uncertain else "KNOWN","new_task_auto_spend":False,"decision":"PARENT_REQUIRED" if uncertain else "POLICY_EVALUATE"}
        if kind == "independent_lanes":
            return {"blocked":[x["id"] for x in i["lanes"] if x["state"]=="BLOCKED"],"executable":[x["id"] for x in i["lanes"] if x["state"]=="EXECUTABLE"],"freeze_all":False}
        if kind == "forgotten_lane":
            forgotten=not i["next_transition"] and not i["stop_gate"]
            return {"signal":"FORGOTTEN_LANE" if forgotten else None,"cycle_ok":not forgotten}
        if kind == "review_to_same_writer":
            routable=all([i["review_complete"],i["reviewed_sha_matches"],i["writer_binding_proven"],not i["duplicate_operation"]])
            return {"route":"SAME_WRITER" if routable else "FAIL_CLOSED","create_new_task":False}
        if kind == "correction_new_sha":
            changed=i["old_sha"] != i["new_sha"]
            return {"prior_review":"STALE" if changed else "CURRENT","ci_required":changed,"review_required":changed}
        if kind == "autosafe_untreated":
            failed=bool(i["auto_safe_incident_present"] and not i["treated_by_cycle_end"])
            return {"cycle":"CONTROL_CYCLE_FAILED" if failed else "OK"}
        if kind == "activation_missing":
            state=i.get("activation_state")
            return {"activation_state":state if state in {"SHADOW","CANARY","ACTIVE_AUTO_SAFE"} else "SHADOW"}
        if kind == "required_ci_missing":
            required=set(i["required_checks"]); seen={x.get("name") for x in i["check_runs"]}
            missing=sorted(required-seen)
            return {"decision":"NOT_A_PASS" if missing else "PASS","signal":"REQUIRED_CI_MISSING" if missing else None,"pass_authorized":not missing}
        if kind == "artifact_attempt_stale":
            clean=i["candidate_attempt"]==i["artifact_attempt"]
            return {"binding":"CLEAN" if clean else "MISMATCH","decision":"ACCEPT" if clean else "REJECT_EVIDENCE","reason":None if clean else "ARTIFACT_RUN_ATTEMPT_MISMATCH"}
        if kind in {"heuristic_session_binding","explicit_session_binding"}:
            explicit=bool(i["explicit_source_binding"] and i["source_repository_proven"] and len(i["candidates"])==1)
            return {"writer_binding":"PROVEN" if explicit else "PROPOSED_UNVERIFIED","decision":"CONTINUE" if explicit else "FAIL_CLOSED"}
        if kind == "duplicate_session_across_lanes":
            sessions=[x["session_id"] for x in i["lanes"]]; duplicate=len(sessions)!=len(set(sessions))
            return {"binding":"AMBIGUOUS" if duplicate else "PROVEN","mutation_allowed":not duplicate,"signal":"DUPLICATE_SESSION_ACROSS_LANES" if duplicate else None}
        if kind == "cross_project_workstream_identity":
            keys=[f'{x["project"]}|{x["route"]}|{x["workstream"]}' for x in i["lanes"]]
            collision=len(keys)!=len(set(keys))
            return {"lane_keys":keys,"collision":collision,"independent":not collision}
        if kind == "binding_drift":
            drift=[]
            if i["previous"]["base_ref"] != i["observed"]["base_ref"]: drift.append("BASE")
            if i["previous"]["head_sha"] != i["observed"]["head_sha"]: drift.append("HEAD")
            if i["previous"]["scope"] != i["observed"]["scope"]: drift.append("SCOPE")
            return {"state":"STALE" if drift else "CURRENT","decision":"RECONCILE_BEFORE_ACTION" if drift else "CONTINUE","drift":drift}
        if kind == "mixed_ci_root_causes":
            cats=[]
            for f in i["failures"]:
                if f["origin"]=="candidate" and f.get("stage")=="test": cats.append("CANDIDATE_TEST_DEFECT")
                elif f["origin"]=="infrastructure" and f.get("transient") is False and f.get("retry_count",0)>=1: cats.append("INFRASTRUCTURE_PERSISTENT")
                else: cats.append("UNKNOWN_REQUIRES_TRIAGE")
            return {"classifications":cats,"collapsed_to_single_root":len(set(cats))==1,"independent_lane_progress":True}
        if kind == "cascaded_failure_collapse":
            ids=[]
            for f in i["failures"]:
                if f["incident_id"] not in ids: ids.append(f["incident_id"])
            return {"shared_blockers":ids,"correction_tasks":0,"duplicate_corrections":False}
        if kind == "closed_unmerged_pr":
            accepted=i["state"]=="open" and bool(i["merged"])
            return {"accepted":accepted,"integrated":bool(i["merged"]),"decision":"ACCEPTED" if accepted else "NOT_ACCEPTED"}
        if kind == "awaiting_plan_approval":
            return {"authority":"PARENT_REQUIRED","send_message":False,"blanket_approve":False,"decision":"STOP_GATE"}
        if kind == "keyword_false_positive":
            classified="UNCLASSIFIED" if not i["structured_signals"] else "STRUCTURED"
            return {"classification":classified,"authority":"DENY" if classified=="UNCLASSIFIED" else "POLICY_EVALUATE","unsafe_shortcut":False}
        if kind == "shadow_trigger_enforcement":
            mode="SHADOW" if not i.get("activation_authority") else str(i["activation_authority"])
            return {"activation_state":mode,"mutation_allowed":mode!="SHADOW"}
        if kind == "browser_evidence_missing":
            missing=bool(i["browser_profile_required"] and not i["browser_profile_ran"])
            return {"accepted":bool(i["pass_text"] and i["exact_head_ci_clean"] and not missing),"decision":"EVIDENCE_INCOMPLETE" if missing else "PARENT_REVIEW_PENDING","signal":"REQUIRED_BROWSER_EVIDENCE_MISSING" if missing else None}
        if kind == "restart_after_send":
            delivered=any(a.get("name") not in set(i["pre_activity_ids"]) and (a.get("userMessaged") or {}).get("userMessage")==i["expected_message"] for a in i["post_activities"])
            return {"readback_required":True,"second_mutation":False,"verdict":"WRITE_CONFIRMED_BY_ACTIVITY" if delivered else "WRITE_NOT_OBSERVED_AFTER_AUTHORITATIVE_READ"}
        if kind == "correction_semantic_regression":
            stale=bool(i["prior_reviewed_sha"] and i["prior_reviewed_sha"]!=i["new_candidate_sha"])
            ci_exact=i["ci_evidence_sha"]==i["new_candidate_sha"]
            return {"prior_review":"STALE" if stale else "CURRENT","accepted":False,"next_action":"RE_REVIEW" if stale and ci_exact else "RECONCILE","exact_new_sha_ci":ci_exact}
        if kind == "waiting_answer_effect_dedup":
            return {"same_effect_identity":True,"decision":"OPERATION_ID_COLLISION","second_send":False}
        if kind == "canary_grant_mismatch":
            allowed=i["grant"]==i["attempt"]
            return {"allowed":allowed,"decision":"ALLOW_CANARY" if allowed else "DENY_CANARY_GRANT_MISMATCH"}
        if kind == "project_specific_environment_policy":
            allowed=bool(i["policy_permits"] and i["bounded_workaround_authorized"] and i["exact_state_read"] and i["latest_activity_read"])
            return {"classification":"ENVIRONMENT_MISMATCH","authority":"AUTO_SAFE" if allowed else "PARENT_REQUIRED","next_action":"CONTINUE_SAME_SESSION" if allowed else "STOP_GATE"}
        if kind == "non_autosafe_blockers_cycle":
            unresolved=[x["id"] for x in i["lanes"] if x.get("authority")=="AUTO_SAFE" and x.get("blocked")]
            return {"cycle":"CONTROL_CYCLE_FAILED" if unresolved else "CONTROL_CYCLE_OK","unresolved_auto_safe":unresolved}
        raise AssertionError(f"Unhandled replay kind: {kind}")
