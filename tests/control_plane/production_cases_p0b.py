from __future__ import annotations

from tests.control_plane.production_adapter_base import *


class P0CasesBMixin:

    def _eval_awaiting_plan_approval(self, i):
            j=self._module("ues.providers.jules"); base=self._module("ues.providers.base")
            class Transport:
                def __init__(self): self.posts=0
                def request(self,method,url,*,headers,body,timeout):
                    if method=="POST": self.posts+=1; return base.HttpResponse(403,{})
                    if "/activities" in url: return base.HttpResponse(200,{},json.dumps({"activities":[]}).encode())
                    return base.HttpResponse(200,{},json.dumps({"name":i["session_id"],"state":i["state"]}).encode())
            t=Transport(); client=j.JulesClient("fixture-key",transport=t,sleeper=lambda _:None)
            caught=None
            try: client.send_message(i["session_id"],"fixture prompt")
            except Exception as exc: caught=exc
            token=f"{getattr(caught,'category','')} {caught or ''}"
            authority="PARENT_REQUIRED" if "PARENT" in token.upper() else "UNPROVEN"
            return {"authority":authority,"send_message":t.posts>0,"blanket_approve":False,"decision":"STOP_GATE" if t.posts==0 and authority=="PARENT_REQUIRED" else "UNSAFE_OR_UNCLASSIFIED"}

    def _eval_keyword_false_positive(self, i):
            r=self._module("ues.routing")
            fn=self._semantic_callable(r,"structured waiting-input classifier",("classify_waiting_activity","classify_waiting_input","classify_waiting"))
            try: out=fn(dict(i))
            except TypeError as exc: raise IntegrationBindingUnavailable(f"waiting classifier cannot consume fixture snapshot: {exc}") from exc
            if not isinstance(out,Mapping): raise IntegrationBindingUnavailable("waiting classifier did not return mapping")
            classification=_enum_value(out.get("classification") or out.get("waiting_class")); authority=_enum_value(out.get("authority")); shortcut=bool(out.get("keyword_shortcut_used") or out.get("unsafe_shortcut"))
            return {"classification":classification,"authority":authority,"unsafe_shortcut":shortcut}

    def _eval_shadow_trigger_enforcement(self, i):
            s=self._module("ues.state_store"); Store=getattr(s,"DeterministicFileStateStore",None)
            if Store is None: raise IntegrationBindingUnavailable("state store replay backend unavailable")
            with tempfile.TemporaryDirectory() as d:
                read=Store(Path(d)/"missing.json").read_workstream("W01")
            return {"activation_state":getattr(read,"effective_activation_mode",None),"mutation_allowed":bool(getattr(read,"mutation_allowed",True))}

    def _eval_browser_evidence_missing(self, i):
            rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
            extra={"lifecycle_state":getattr(lifecycle.LifecycleState,"REVIEW_RESULT"),"review":self._review_binding(),"ci":self._ci_binding(),"browser_profile_required":i["browser_profile_required"],"browser_profile_ran":i["browser_profile_ran"],"browser_evidence_required":i["browser_profile_required"],"browser_evidence_complete":i["browser_profile_ran"]}
            out=self._callable(rec,"reconcile_workstream")(self._base_binding(**extra)); issues=getattr(out,"issues",())
            missing=_contains_token(issues,"BROWSER") or _contains_token(out,"REQUIRED_BROWSER_EVIDENCE_MISSING")
            action=_enum_value(getattr(getattr(out,"resolution",None),"action",None)); accepted=not missing and action=="REQUEST_PARENT_REVIEW"
            return {"accepted":accepted,"decision":"EVIDENCE_INCOMPLETE" if missing else "PARENT_REVIEW_PENDING","signal":"REQUIRED_BROWSER_EVIDENCE_MISSING" if missing else None}

    def _eval_restart_after_send(self, i):
            r=self._module("ues.recovery")
            out=self._callable(r,"reconcile_provider_write")({"authoritative_read_complete":True,"post_session_state":i["session_state"],"pre_activity_ids":i["pre_activity_ids"],"post_activities":i["post_activities"],"expected_user_message":i["expected_message"],"write_outcome":"WRITE_OUTCOME_UNKNOWN"})
            return {"readback_required":True,"second_mutation":bool(out.get("safe_to_blind_retry")),"verdict":out.get("verdict")}

    def _eval_correction_semantic_regression(self, i):
            r=self._module("ues.routing")
            out=self._callable(r,"route_writer_to_reviewer")(prior_reviewed_sha=i["prior_reviewed_sha"],new_candidate_sha=i["new_candidate_sha"],ci_evidence_sha=i["ci_evidence_sha"],review_evidence_sha=i["review_evidence_sha"],existing_reviewer_available=i["existing_reviewer_available"],existing_reviewer_safe_to_reuse=i["existing_reviewer_safe_to_reuse"],new_reviewer_policy_allows=False,parent_gate_satisfied=False)
            action=str(out.get("action") or ""); next_action="RE_REVIEW" if "REVIEWER" in action and action!="STOP" else "STOP"
            exact_ci=out.get("exact_new_sha_ci")
            if exact_ci is None: exact_ci=out.get("exact_new_sha_evidence")
            return {"prior_review":"STALE" if out.get("prior_review_stale") else "CURRENT","accepted":False,"next_action":next_action,"exact_new_sha_ci":bool(exact_ci)}

    def _eval_waiting_answer_effect_dedup(self, i):
            idem=self._module("ues.idempotency"); keyfn=self._callable(idem,"waiting_answer_operation_key"); evalfn=self._callable(idem,"evaluate_idempotency")
            a,b=i["answer_digests"]
            kwargs={"project":i["project"],"workstream_id":i["workstream"],"session_id":i["session_id"],"waiting_activity_id":i["waiting_activity_id"]}
            k1=keyfn(**kwargs,answer_digest=a); k2=keyfn(**kwargs,answer_digest=b)
            out=evalfn(k2,b,[{"operation_id":k1,"request_digest":a,"state":"CONFIRMED"}])
            return {"same_effect_identity":k1==k2,"decision":out.get("decision"),"second_send":bool(out.get("safe_to_execute"))}

    def _eval_canary_grant_mismatch(self, i):
            s=self._module("ues.state_store")
            for name in ("evaluate_canary_grant","authorize_canary_action","evaluate_activation_grant"):
                fn=getattr(s,name,None)
                if callable(fn):
                    out=fn({"mode":i["activation_mode"],"grant":i["grant"],"attempt":i["attempt"]})
                    if not isinstance(out,Mapping): raise IntegrationBindingUnavailable(f"{name} did not return mapping")
                    allowed=bool(out.get("allowed") or out.get("mutation_allowed")); return {"allowed":allowed,"decision":out.get("decision") or ("ALLOW_CANARY" if allowed else "DENY_CANARY_GRANT_MISMATCH")}
            Store=getattr(s,"DeterministicFileStateStore",None); Record=getattr(s,"WorkstreamRuntimeRecord",None)
            if Store is None or Record is None: raise IntegrationBindingUnavailable("canary state APIs unavailable")
            with tempfile.TemporaryDirectory() as d:
                store=Store(Path(d)/"state.json"); store.initialize(); record=_construct(Record,{"workstream_id":i["attempt"]["workstream"],"project":i["attempt"]["project"],"activation_mode":"CANARY","canary_grant":i["grant"],"activation_grant":i["grant"]}); store.compare_and_swap_workstream(i["attempt"]["workstream"],0,record); read=store.read_workstream(i["attempt"]["workstream"])
            allowed=bool(getattr(read,"mutation_allowed",False)); return {"allowed":allowed,"decision":"ALLOW_CANARY" if allowed else "DENY_CANARY_GRANT_MISMATCH"}

    def _eval_project_specific_environment_policy(self, i):
            r=self._module("ues.routing"); fn=self._callable(r,"route_waiting")
            kwargs={"exact_state_read":i["exact_state_read"],"latest_activity_read":i["latest_activity_read"],"project_policy_permits":i["policy_permits"],"bounded_workaround_authorized":i["bounded_workaround_authorized"]}
            sig=inspect.signature(fn)
            if "project" in sig.parameters: kwargs["project"]=i["project"]
            out=fn("ENVIRONMENT_MISMATCH",**kwargs)
            authority=out.get("authority"); action=out.get("action")
            return {"classification":out.get("waiting_class"),"authority":authority,"next_action":"CONTINUE_SAME_SESSION" if action=="CONTINUE_SAME_SESSION" else "STOP_GATE"}

    def _eval_non_autosafe_blockers_cycle(self, i):
            wd=self._module("ues.watchdog")
            lanes=[{"lane_id":x["id"],"authority":x["authority"],"blocked":x["blocked"],"stop_gate":x["authority"]} for x in i["lanes"]]
            out=self._callable(wd,"evaluate_control_cycle")(lanes)
            return {"cycle":out.get("cycle_status"),"unresolved_auto_safe":out.get("unresolved_auto_safe_lanes",[])}
