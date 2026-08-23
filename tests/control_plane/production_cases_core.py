from __future__ import annotations

from tests.control_plane.production_adapter_base import *


class CoreCasesMixin:

    def _eval_waiting_stale(self, i):
            wd=self._module("ues.watchdog")
            out=self._callable(wd,"evaluate_lane_watchdog")({"lane_id":"fixture","waiting_class":"POLICY_RESOLVABLE","waiting_age_seconds":i["waiting_age_seconds"],"authority":i["authority"],"auto_safe_incident":True,"auto_safe_treated":False,"next_action":"CONTINUE_SAME_SESSION"},thresholds={"waiting_seconds":i["threshold_seconds"]})
            stale=any(x.get("code") in {"WAITING_TOO_LONG","WAITING_INPUT_STALE"} for x in out.get("incidents",[]))
            return {"status":"INCIDENT" if stale else "WAITING","signal":"WAITING_INPUT_STALE" if stale else "WAITING_INPUT","authority":i["authority"],"next_action":"CONTINUE_SAME_SESSION" if stale and i["authority"]=="AUTO_SAFE" else "NONE"}

    def _eval_environment_mismatch(self, i):
            r=self._module("ues.routing")
            out=self._callable(r,"route_waiting")("ENVIRONMENT_MISMATCH",exact_state_read=i["exact_state_read"],latest_activity_read=i["latest_activity_read"],project_policy_permits=i["project_policy_permits"],bounded_workaround_authorized=i["bounded_workaround_authorized"])
            return {"classification":out.get("waiting_class"),"authority":out.get("authority"),"next_action":"CONTINUE_SAME_SESSION" if out.get("action")=="CONTINUE_SAME_SESSION" else "STOP_GATE"}

    def _eval_binding_mismatch(self, i):
            out=self._session_binding({"expected_channel":i["expected_channel"],"observed_channel":i["observed_channel"],"candidates":[],"explicit_source_binding":False,"source_repository_proven":False})
            binding=out["writer_binding"]
            return {"binding":binding,"decision":out["decision"],"signal":"SESSION_CHANNEL_BINDING_MISMATCH" if binding=="MISMATCH" else None}

    def _eval_terminal_session(self, i):
            r=self._module("ues.routing")
            out=self._callable(r,"route_terminal_session_failure")(same_session_available=i["continuation_available"])
            return {"state":i["normalized_state"],"signal":out.get("classification"),"recommendation":out.get("action"),"auto_create_task":bool(out.get("automatic_new_task_creation"))}

    def _eval_reviewer_mutation(self, i):
            r=self._module("ues.routing")
            out=self._callable(r,"route_reviewer_to_writer")(workstream_id="W01",reviewed_sha="1"*40,candidate_sha="1"*40,reviewer_role_valid=True,reviewer_independent=True,reviewer_mutation_detected=i["reviewer_code_diff"],reviewer_mutation_adjudicated=True,reviewer_mutation_disqualifying=True,writer_binding_proven=True,finding_within_writer_scope=True,correction_in_flight=False,correction_already_sent=False,findings=[{"id":"F1","root_cause":"fixture","summary":"fixture","paths":["x"]}])
            invalid=bool(i["reviewer_code_diff"] and (out.get("action")=="STOP" or _contains_token(out,"REVIEWER_MUTATION")))
            return {"review_valid":not invalid,"signal":"REVIEWER_MUTATION_DETECTED" if invalid else None,"route_findings":bool(out.get("reuse_existing_writer"))}

    def _eval_pass_without_evidence(self, i):
            rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
            b=self._base_binding(lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"),review=self._review_binding(reviewed_sha="1"*40),ci=self._ci_binding(run_id=None,artifact_id=None,candidate_sha="1"*40))
            out=self._callable(rec,"reconcile_workstream")(b)
            incomplete=bool(getattr(out,"issues",())) or _contains_token(getattr(out,"resolution",None),"INCOMPLETE")
            action=_enum_value(getattr(getattr(out,"resolution",None),"action",None))
            accepted=not incomplete and action=="REQUEST_PARENT_REVIEW"
            return {"accepted":accepted,"decision":"EVIDENCE_INCOMPLETE" if incomplete else "PARENT_REVIEW_PENDING"}

    def _eval_ambiguous_writer_binding(self, i):
            return self._session_binding({"candidates":[{"session_id":x} for x in i["candidate_writer_sessions"]],"explicit_source_binding":i.get("explicit_source_binding",False),"source_repository_proven":False})

    def _eval_candidate_sha_moved(self, i):
            rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
            prev=self._base_binding(head_sha=i["reviewed_sha"],review=self._review_binding(reviewed_sha=i["reviewed_sha"]),lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"))
            cur=self._base_binding(head_sha=i["current_candidate_sha"],review=self._review_binding(reviewed_sha=i["reviewed_sha"]),lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"))
            out=self._callable(rec,"reconcile_workstream")(cur,prev)
            stale=bool(getattr(out,"candidate_sha_moved",False) or getattr(out,"prior_review_invalidated",False))
            return {"prior_review":"STALE" if stale else "CURRENT","next_action":"RE_CI_THEN_REVIEW" if stale else "NONE"}

    def _eval_ci_artifact_attempt_mismatch(self, i):
            out=self._workflow_binding(i)
            clean=bool(out.get("binding_valid")) and not out.get("artifact_mismatches")
            return {"binding":"CLEAN" if clean else "MISMATCH","decision":"ACCEPT" if clean else "REJECT_EVIDENCE"}

    def _eval_provider_failure_matrix(self, i):
            fmod=self._module("ues.failures"); fn=self._callable(fmod,"classify_provider_failure")
            mapping={"PROVIDER_AUTHENTICATION":"AUTHORIZATION_FAILURE","PROVIDER_AUTHORIZATION":"AUTHORIZATION_FAILURE","PROVIDER_RATE_LIMIT":"RATE_LIMITED","PROVIDER_SERVER_ERROR":"PROVIDER_SERVER_FAILURE","PROVIDER_NETWORK_ERROR":"NETWORK_FAILURE"}
            rows=[]
            for item in i["failures"]:
                payload={"status_code":item.get("status"),"network_error":item["kind"]=="network"}
                out=fn(payload); rows.append({"kind":item["kind"],"class":mapping.get(out.get("category"),out.get("category")),"blind_retry":bool(out.get("safe_to_blind_retry"))})
            return {"failures":rows}

    def _eval_unknown_jules_state(self, i):
            j=self._module("ues.providers.jules")
            return {"normalized_state":self._callable(j,"normalize_session_state")(i["raw_state"])}

    def _eval_ambiguous_write(self, i):
            r=self._module("ues.recovery")
            out=self._callable(r,"reconcile_provider_write")({"authoritative_read_complete":False,"post_session_state":"UNKNOWN","pre_activity_ids":[],"post_activities":[],"expected_user_message":"fixture"})
            return {"write_outcome":"AMBIGUOUS","blind_retry":bool(out.get("safe_to_blind_retry")),"next_action":"READ_AUTHORITATIVE_POST_STATE" if _contains_token(out,"AUTHORITATIVE_READ") else str(out.get("retry_consideration"))}

    def _eval_duplicate_correction(self, i):
            idem=self._module("ues.idempotency"); fn=self._callable(idem,"evaluate_idempotency")
            key=i["operation_key"]; recs=[{"operation_id":x,"request_digest":"fixture","state":"IN_FLIGHT"} for x in i["existing_operation_keys"]]
            out=fn(key,"fixture",recs); duplicate=not bool(out.get("safe_to_execute"))
            return {"duplicate":duplicate,"decision":"SUPPRESS_DUPLICATE_OPERATION" if duplicate else "RESERVE_OPERATION"}

    def _eval_task_budget_uncertain(self, i):
            tb=self._module("ues.task_budget")
            out=self._callable(tb,"evaluate_task_budget")(project="FIXTURE",ceiling=10,reserve=2,lifetime_consumption_known=i["lifetime_usage_known"],proven_lifetime_used=None,current_enumerated_tasks=i["current_enumeration"])
            return {"current_enumeration":out.get("current_enumerated_tasks"),"lifetime_budget":"UNKNOWN" if _contains_token(out,"UNKNOWN_LIFETIME") else "KNOWN","new_task_auto_spend":bool(out.get("automatic_new_task_creation")),"decision":"PARENT_REQUIRED" if not out.get("budget_allows_new_task") else "POLICY_EVALUATE"}

    def _eval_independent_lanes(self, i):
            wd=self._module("ues.watchdog")
            lanes=[{"lane_id":x["id"],"blocked":x["state"]=="BLOCKED","next_action":"EXECUTE" if x["state"]=="EXECUTABLE" else None,"stop_gate":"BLOCKED" if x["state"]=="BLOCKED" else None} for x in i["lanes"]]
            out=self._callable(wd,"evaluate_control_cycle")(lanes)
            return {"blocked":out.get("blocked_lanes",[]),"executable":out.get("executable_lanes",[]),"freeze_all":bool(out.get("blocked_lane_freezes_independent_lanes"))}

    def _eval_forgotten_lane(self, i):
            wd=self._module("ues.watchdog")
            out=self._callable(wd,"evaluate_lane_watchdog")({"lane_id":"fixture","next_action":i["next_transition"],"stop_gate":i["stop_gate"]})
            forgotten=bool(out.get("forgotten")) or _contains_token(out,"FORGOTTEN_LANE")
            return {"signal":"FORGOTTEN_LANE" if forgotten else None,"cycle_ok":not forgotten}

    def _eval_review_to_same_writer(self, i):
            r=self._module("ues.routing")
            out=self._callable(r,"route_reviewer_to_writer")(workstream_id="W01",reviewed_sha="1"*40,candidate_sha="1"*40,reviewer_role_valid=True,reviewer_independent=True,reviewer_mutation_detected=False,reviewer_mutation_adjudicated=True,reviewer_mutation_disqualifying=False,writer_binding_proven=i["writer_binding_proven"],finding_within_writer_scope=True,correction_in_flight=i["duplicate_operation"],correction_already_sent=False,findings=[{"id":"F1","root_cause":"fixture","summary":"fixture","paths":[]}])
            return {"route":"SAME_WRITER" if out.get("reuse_existing_writer") else "FAIL_CLOSED","create_new_task":bool(out.get("automatic_new_task_creation"))}

    def _eval_correction_new_sha(self, i):
            moved=i["old_sha"]!=i["new_sha"]
            rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
            prev=self._base_binding(head_sha=i["old_sha"],review=self._review_binding(reviewed_sha=i["old_sha"]),lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"))
            cur=self._base_binding(head_sha=i["new_sha"],review=self._review_binding(reviewed_sha=i["old_sha"]),lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"))
            out=self._callable(rec,"reconcile_workstream")(cur,prev)
            stale=bool(getattr(out,"candidate_sha_moved",False) or getattr(out,"prior_review_invalidated",False))
            return {"prior_review":"STALE" if stale else "CURRENT","ci_required":stale,"review_required":stale}

    def _eval_autosafe_untreated(self, i):
            wd=self._module("ues.watchdog")
            out=self._callable(wd,"evaluate_control_cycle")([{"lane_id":"auto-safe","authority":"AUTO_SAFE","auto_safe_incident":i["auto_safe_incident_present"],"auto_safe_treated":i["treated_by_cycle_end"],"next_action":"CONTINUE"}])
            return {"cycle":out.get("cycle_status")}

    def _eval_activation_missing(self, i):
            s=self._module("ues.state_store"); Store=getattr(s,"DeterministicFileStateStore",None)
            if Store is None: raise IntegrationBindingUnavailable("ues.state_store.DeterministicFileStateStore unavailable for missing-state SHADOW replay")
            with tempfile.TemporaryDirectory() as d:
                read=Store(Path(d)/"missing.json").read_workstream("W01")
            return {"activation_state":getattr(read,"effective_activation_mode",None)}
