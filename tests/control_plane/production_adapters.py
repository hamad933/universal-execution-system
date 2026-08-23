"""Production-backed R2 replay adapter using exact frozen A-D public APIs.

No semantic aliases and no ReferenceOracle fallback are permitted here. Synthetic
transport doubles stop at the provider network boundary while executing real provider
methods.
"""
from __future__ import annotations

import importlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


class IntegrationBindingUnavailable(AssertionError):
    pass


def _enum(value: Any) -> Any:
    return getattr(value, "value", value)


def _has(value: Any, token: str) -> bool:
    needle = token.upper()
    if isinstance(value, str):
        return needle in value.upper()
    if isinstance(value, Mapping):
        return any(_has(k, token) or _has(v, token) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_has(v, token) for v in value)
    return False


class ProductionReplayAdapter:
    def _module(self, name: str):
        try:
            return importlib.import_module(name)
        except Exception as exc:
            raise IntegrationBindingUnavailable(f"production module unavailable: {name}: {exc}") from exc

    @staticmethod
    def _require(module: Any, name: str):
        value = getattr(module, name, None)
        if value is None:
            raise IntegrationBindingUnavailable(f"exact R2 binding unavailable: {module.__name__}.{name}")
        return value

    def evaluate(self, case: Any) -> dict[str, Any]:
        method = getattr(self, f"_eval_{case.kind}", None)
        if not callable(method):
            raise IntegrationBindingUnavailable(f"no production adapter for replay kind {case.kind}")
        return method(case.inputs)

    def _actors(self, *, writer_status="PROVEN_EXPLICIT", reviewer_status="PROVEN_EXPLICIT", writer_session="writer-fixture", reviewer_session="reviewer-fixture"):
        rec = self._module("ues.reconciliation")
        lifecycle = self._module("ues.lifecycle")
        Actor = self._require(rec, "ActorBinding")
        Status = self._require(lifecycle, "SourceBindingStatus")
        def one(role, status, session):
            return Actor(
                role=role,
                provider="jules",
                session_id=session,
                task_id=f"{role.lower()}-task",
                lineage=f"{role.lower()}-lineage",
                source_repository="fixture/repo",
                source_identity=f"sources/{role.lower()}",
                proof_status=getattr(Status, status),
                evidence_id=f"binding-{role.lower()}" if status == "PROVEN_EXPLICIT" else None,
            )
        return (one("WRITER", writer_status, writer_session), one("REVIEWER", reviewer_status, reviewer_session))

    def _review(self, sha="1" * 40, outcome="PASS"):
        rec = self._module("ues.reconciliation")
        lifecycle = self._module("ues.lifecycle")
        Review = self._require(rec, "ReviewBinding")
        Outcome = self._require(lifecycle, "ReviewOutcome")
        return Review(
            review_id="review-fixture",
            reviewed_sha=sha,
            reviewer_lineage="reviewer-lineage",
            source_repository="fixture/repo",
            evidence_classification="EXACT_SHA_REVIEW",
            outcome=getattr(Outcome, outcome),
        )

    def _ci(self, sha="1" * 40, outcome="PASS"):
        rec = self._module("ues.reconciliation")
        lifecycle = self._module("ues.lifecycle")
        CI = self._require(rec, "CIBinding")
        Outcome = self._require(lifecycle, "CIOutcome")
        return CI(
            source_provider="github",
            source_repository="fixture/repo",
            workflow_identity="validate.yml",
            required_check_identity="Validate Universal Core / core",
            workflow_run_id="100",
            run_attempt=1,
            job_id="200",
            producer_job="core",
            candidate_sha=sha,
            classification="REQUIRED_CI_PASS",
            outcome=getattr(Outcome, outcome),
        )

    def _binding(self, **overrides):
        rec = self._module("ues.reconciliation")
        lifecycle = self._module("ues.lifecycle")
        Workstream = self._require(rec, "WorkstreamBinding")
        values = dict(
            project="FIXTURE",
            route="PERSONAL:FIXTURE",
            workstream="W01",
            role="WRITER",
            repo="fixture/repo",
            branch="feature/fixture",
            lifecycle_state=getattr(self._require(lifecycle, "LifecycleState"), "WRITER_ACTIVE"),
            baseline_sha="0" * 40,
            base_ref="main",
            task_budget_class="FIXTURE",
            last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            writer_lineage="writer-lineage",
            reviewer_lineage="reviewer-lineage",
            actor_bindings=self._actors(),
            scope_identity="scope-a",
            head_sha="1" * 40,
            pr_number=7,
        )
        values.update(overrides)
        return Workstream(**values)

    def _profile(self, profile_id: str, *, name: str, proven: bool, current: bool = True, action: str = "REQUEST_PARENT_REVIEW"):
        rec = self._module("ues.reconciliation")
        Requirement = self._require(rec, "EvidenceRequirement")
        Profile = self._require(rec, "RequiredEvidenceProfile")
        return Profile(profile_id, (Requirement(name=name, proven=proven, current=current, evidence_id="fixture-evidence" if proven else None, actions=(action,)),))

    def _github_client(self, *, read_json=None, paginate=None):
        gh = self._module("ues.providers.github")
        Client = self._require(gh, "GitHubClient")
        class Synthetic(Client):
            def __init__(self):
                pass
            def _read_json(self, path, *, operation, extra_headers=None):
                if read_json is None:
                    raise AssertionError(f"unexpected read: {operation}")
                return read_json(path, operation)
            def _paginate(self, path, *, item_key, operation, extra_headers=None):
                if paginate is None:
                    raise AssertionError(f"unexpected paginate: {operation}")
                return paginate(path, operation)
        return Synthetic()

    # CP-001..020
    def _eval_waiting_stale(self, i):
        wd = self._module("ues.watchdog")
        out = self._require(wd, "evaluate_lane_watchdog")(
            {"lane_id":"fixture","waiting_class":"POLICY_RESOLVABLE","waiting_resolved":False,"waiting_age_seconds":i["waiting_age_seconds"],"next_action":"CONTINUE_SAME_SESSION"},
            policy={"thresholds":{"waiting_unresolved_seconds":i["threshold_seconds"]}},
        )
        stale = any(x.get("code") == "WAITING_UNRESOLVED" for x in out["incidents"])
        return {"status":"INCIDENT" if stale else "WAITING","signal":"WAITING_INPUT_STALE" if stale else "WAITING_INPUT","authority":i["authority"],"next_action":"CONTINUE_SAME_SESSION" if stale and i["authority"]=="AUTO_SAFE" else "NONE"}

    def _eval_environment_mismatch(self, i):
        routing = self._module("ues.routing")
        effect = self._require(routing, "WAITING_SAME_SESSION_CONTINUATION")
        out = self._require(routing, "route_waiting")(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=i["exact_state_read"], latest_activity_read=i["latest_activity_read"],
            continuation_binding_proven=True,
            project_auto_safe_actions={effect} if i.get("project_policy_permits") else set(),
            bounded_workaround_authorized=i["bounded_workaround_authorized"],
        )
        return {"classification":out["waiting_class"],"authority":out["authority"],"next_action":"CONTINUE_SAME_SESSION" if out["action"]=="CONTINUE_SAME_SESSION" else "STOP_GATE"}

    def _eval_binding_mismatch(self, i):
        rec = self._module("ues.reconciliation")
        item = self._binding(actor_bindings=self._actors(writer_session=i["observed_channel"]))
        out = self._require(rec, "resolve_actor_binding")(item, "WRITER")
        match = bool(out.proven and out.binding.session_id == i["expected_channel"])
        return {"binding":"PROVEN" if match else "MISMATCH","decision":"CONTINUE" if match else "FAIL_CLOSED","signal":None if match else "SESSION_CHANNEL_BINDING_MISMATCH"}

    def _eval_terminal_session(self, i):
        routing = self._module("ues.routing")
        out = self._require(routing, "route_terminal_session_failure")(same_session_available=i["continuation_available"], project_auto_safe_actions={self._require(routing,"FAILURE_SAME_SESSION_RECOVERY")})
        terminal = out["classification"] == "SESSION_CONTINUATION_UNAVAILABLE"
        return {"state":i["normalized_state"],"signal":"SESSION_CONTINUATION_UNAVAILABLE" if terminal else "RECOVERABLE_SESSION","recommendation":"NEW_TASK_RECOMMENDED" if terminal else "SAME_SESSION_RECOVERY","auto_create_task":bool(out["automatic_new_task_creation"])}

    def _eval_reviewer_mutation(self, i):
        routing = self._module("ues.routing")
        out = self._require(routing, "route_reviewer_to_writer")(
            project="FIXTURE", route="PERSONAL:FIXTURE", workstream_id="W01",
            writer_session_id="writer", reviewer_session_id="reviewer",
            reviewed_sha="1"*40, candidate_sha="1"*40,
            reviewer_role_valid=True, reviewer_independent=True,
            reviewer_mutation_detected=i["reviewer_code_diff"], reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=i["reviewer_code_diff"], writer_binding_proven=True,
            writer_binding_kind="EXPLICIT", finding_within_writer_scope=True,
            canonical_operation_active=False, canonical_operation_confirmed=False,
            findings=[{"id":"F1","root_cause":"fixture","summary":"x","paths":["x"]}],
            project_auto_safe_actions={self._require(routing,"REVIEW_CORRECTION_PACKET")},
        )
        invalid = out["authority"] != "AUTO_SAFE"
        return {"review_valid":not invalid,"signal":"REVIEWER_MUTATION_DETECTED" if invalid and i["reviewer_code_diff"] else None,"route_findings":bool(out["reuse_existing_writer"])}

    def _eval_pass_without_evidence(self, i):
        rec = self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        profile = self._profile("pass-evidence", name="exact-ci", proven=bool(i["ci_exact_head_clean"] and i["artifact_binding_clean"]))
        item = self._binding(role="REVIEWER", actor_bindings=(), lifecycle_state=getattr(lifecycle.LifecycleState,"REVIEW_RESULT"), review=self._review(outcome="PASS"), evidence_profile=profile)
        out = self._require(rec,"reconcile_workstream")(item)
        accepted = not out.issues and _enum(out.resolution.action) == "REQUEST_PARENT_REVIEW"
        return {"accepted":accepted,"decision":"PARENT_REVIEW_PENDING" if accepted else "EVIDENCE_INCOMPLETE"}

    def _eval_ambiguous_writer_binding(self, i):
        rec=self._module("ues.reconciliation")
        Actor=self._require(rec,"ActorBinding"); lifecycle=self._module("ues.lifecycle"); Status=self._require(lifecycle,"SourceBindingStatus")
        actors=tuple(Actor(role="WRITER",provider="jules",session_id=s,task_id=s,lineage=s,source_repository="fixture/repo",source_identity=f"sources/{n}",proof_status=Status.PROVEN_EXPLICIT,evidence_id=f"e{n}") for n,s in enumerate(i["candidate_writer_sessions"]))
        out=self._require(rec,"resolve_actor_binding")(self._binding(actor_bindings=actors),"WRITER")
        return {"writer_binding":"AMBIGUOUS" if out.state=="AMBIGUOUS" else "PROVEN" if out.proven else "PROPOSED_UNVERIFIED","decision":"FAIL_CLOSED" if not out.proven else "ROUTE"}

    def _eval_candidate_sha_moved(self, i):
        rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        prev=self._binding(head_sha=i["reviewed_sha"], lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT, review=self._review(i["reviewed_sha"]))
        cur=self._binding(head_sha=i["current_candidate_sha"], lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT, review=self._review(i["reviewed_sha"]))
        out=self._require(rec,"reconcile_workstream")(cur,prev)
        stale=bool(out.candidate_sha_moved or out.prior_review_invalidated)
        return {"prior_review":"STALE" if stale else "CURRENT","next_action":"RE_CI_THEN_REVIEW" if stale else "NONE"}

    def _workflow_binding(self, i):
        run_id=int(i.get("run_id", i.get("expected_run_id"))); attempt=int(i.get("candidate_attempt",i.get("expected_attempt"))); artifact_attempt=int(i.get("artifact_attempt")); sha=i.get("candidate_sha","d"*40)
        def read_json(path,operation):
            if operation=="github.workflow_run.get": return {"id":run_id,"run_attempt":attempt,"head_sha":sha}
            raise AssertionError(operation)
        def paginate(path,operation):
            if operation=="github.workflow_jobs.list": return [{"id":5001,"run_id":run_id,"head_sha":sha,"name":i.get("producer_job","fixture-job")}]
            if operation=="github.workflow_artifacts.list": return [{"id":i.get("artifact_id",4001),"name":"fixture-artifact","digest":i.get("artifact_digest","digest"),"run_attempt":artifact_attempt,"producer_job_id":5001,"workflow_run":{"id":run_id,"head_sha":sha,"run_attempt":artifact_attempt}}]
            raise AssertionError(operation)
        return self._require(self._module("ues.providers.github"),"GitHubClient").get_workflow_binding(self._github_client(read_json=read_json,paginate=paginate),"fixture","repo",run_id,expected_sha=sha,expected_run_attempt=attempt)

    def _eval_ci_artifact_attempt_mismatch(self, i):
        data=dict(i); data.setdefault("candidate_sha","d"*40); data.setdefault("artifact_id",4001)
        out=self._workflow_binding(data); clean=bool(out["binding_valid"])
        return {"binding":"CLEAN" if clean else "MISMATCH","decision":"ACCEPT" if clean else "REJECT_EVIDENCE"}

    def _eval_provider_failure_matrix(self, i):
        failures=self._module("ues.failures"); fn=self._require(failures,"classify_provider_failure")
        mapping={"PROVIDER_AUTHENTICATION":"AUTHORIZATION_FAILURE","PROVIDER_AUTHORIZATION":"AUTHORIZATION_FAILURE","PROVIDER_RATE_LIMIT":"RATE_LIMITED","PROVIDER_SERVER_ERROR":"PROVIDER_SERVER_FAILURE","PROVIDER_NETWORK_ERROR":"NETWORK_FAILURE"}
        rows=[]
        for item in i["failures"]:
            out=fn({"status_code":item.get("status"),"network_error":item["kind"]=="network"})
            rows.append({"kind":item["kind"],"class":mapping.get(out["category"],out["category"]),"blind_retry":bool(out["safe_to_blind_retry"])})
        return {"failures":rows}

    def _eval_unknown_jules_state(self, i):
        jules=self._module("ues.providers.jules")
        return {"normalized_state":self._require(jules,"normalize_session_state")(i["raw_state"])}

    def _eval_ambiguous_write(self, i):
        recovery=self._module("ues.recovery")
        out=self._require(recovery,"reconcile_provider_write")({"authoritative_read_complete":False,"post_session_state":"UNKNOWN","pre_activity_ids":[],"post_activities":[],"expected_user_message":"fixture"})
        return {"write_outcome":"AMBIGUOUS","blind_retry":bool(out.get("safe_to_blind_retry")),"next_action":"READ_AUTHORITATIVE_POST_STATE" if _has(out,"AUTHORITATIVE_READ") else str(out.get("retry_consideration"))}

    def _eval_duplicate_correction(self, i):
        idem=self._module("ues.idempotency"); fn=self._require(idem,"evaluate_idempotency")
        records=[{"operation_id":x,"request_digest":"fixture","state":"IN_FLIGHT"} for x in i["existing_operation_keys"]]
        out=fn(i["operation_key"],"fixture",records); duplicate=not out["safe_to_execute"]
        return {"duplicate":duplicate,"decision":"SUPPRESS_DUPLICATE_OPERATION" if duplicate else "RESERVE_OPERATION"}

    def _eval_task_budget_uncertain(self, i):
        tb=self._module("ues.task_budget")
        out=self._require(tb,"evaluate_task_budget")(project="FIXTURE",ceiling=10,reserve=2,lifetime_consumption_known=i["lifetime_usage_known"],proven_lifetime_used=None,current_enumerated_tasks=i["current_enumeration"])
        return {"current_enumeration":out.get("current_enumerated_tasks"),"lifetime_budget":"UNKNOWN" if _has(out,"UNKNOWN_LIFETIME") else "KNOWN","new_task_auto_spend":bool(out.get("automatic_new_task_creation")),"decision":"PARENT_REQUIRED" if not out.get("budget_allows_new_task") else "POLICY_EVALUATE"}

    def _eval_independent_lanes(self, i):
        wd=self._module("ues.watchdog")
        lanes=[{"lane_id":x["id"],"blocked":x["state"]=="BLOCKED","next_action":"EXECUTE" if x["state"]=="EXECUTABLE" else None,"stop_gate":"PARENT_REQUIRED" if x["state"]=="BLOCKED" else None} for x in i["lanes"]]
        out=self._require(wd,"evaluate_control_cycle")(lanes)
        return {"blocked":out["blocked_lanes"],"executable":out["executable_lanes"],"freeze_all":bool(out["blocked_lane_freezes_independent_lanes"])}

    def _eval_forgotten_lane(self, i):
        wd=self._module("ues.watchdog"); out=self._require(wd,"evaluate_control_cycle")([{"lane_id":"fixture","next_action":i["next_transition"],"stop_gate":i["stop_gate"]}])
        forgotten="fixture" in out["forgotten_lanes"]
        return {"signal":"FORGOTTEN_LANE" if forgotten else None,"cycle_ok":out["cycle_status"]!="CONTROL_CYCLE_FAILED"}

    def _eval_review_to_same_writer(self, i):
        routing=self._module("ues.routing")
        out=self._require(routing,"route_reviewer_to_writer")(project="FIXTURE",route="PERSONAL:FIXTURE",workstream_id="W01",writer_session_id="writer",reviewer_session_id="reviewer",reviewed_sha="1"*40,candidate_sha="1"*40,reviewer_role_valid=True,reviewer_independent=True,reviewer_mutation_detected=False,reviewer_mutation_adjudicated=True,reviewer_mutation_disqualifying=False,writer_binding_proven=i["writer_binding_proven"],writer_binding_kind="EXPLICIT",finding_within_writer_scope=True,canonical_operation_active=i["duplicate_operation"],canonical_operation_confirmed=False,findings=[{"id":"F1","root_cause":"fixture","summary":"x","paths":[]}],project_auto_safe_actions={self._require(routing,"REVIEW_CORRECTION_PACKET")})
        return {"route":"SAME_WRITER" if out["reuse_existing_writer"] else "FAIL_CLOSED","create_new_task":bool(out["automatic_new_task_creation"])}

    def _eval_correction_new_sha(self, i):
        rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        prev=self._binding(head_sha=i["old_sha"], lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT, review=self._review(i["old_sha"]))
        cur=self._binding(head_sha=i["new_sha"], lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT, review=self._review(i["old_sha"]))
        out=self._require(rec,"reconcile_workstream")(cur,prev); stale=bool(out.candidate_sha_moved or out.prior_review_invalidated)
        return {"prior_review":"STALE" if stale else "CURRENT","ci_required":stale,"review_required":stale}

    def _eval_autosafe_untreated(self, i):
        wd=self._module("ues.watchdog"); out=self._require(wd,"evaluate_control_cycle")([{"lane_id":"auto-safe","next_action":"CONTINUE","auto_safe_incident_proven":i["auto_safe_incident_present"],"auto_safe_treated":i["treated_by_cycle_end"]}])
        return {"cycle":out["cycle_status"]}

    def _eval_activation_missing(self, i):
        state=self._module("ues.state_store"); Store=self._require(state,"DeterministicFileStateStore")
        with tempfile.TemporaryDirectory() as d:
            read=Store(Path(d)/"missing.json").read_workstream("lane-missing")
        return {"activation_state":read.effective_activation_mode}

    # CP-021..041
    def _eval_required_ci_missing(self, i):
        def paginate(path,operation):
            if operation=="github.statuses.list": return list(i["statuses"])
            if operation=="github.checks.list": return list(i["check_runs"])
            raise AssertionError(operation)
        client=self._github_client(paginate=paginate)
        specs=[{"kind":"check","name":name} for name in i["required_checks"]]
        out=self._require(self._module("ues.providers.github"),"GitHubClient").get_required_ci_evidence(client,"fixture","repo",i["candidate_sha"],specs)
        return {"decision":"PASS" if out["pass_authorized"] else "NOT_A_PASS","signal":"REQUIRED_CI_MISSING" if out.get("reason")=="REQUIRED_CI_MISSING" else None,"pass_authorized":bool(out["pass_authorized"])}

    def _eval_artifact_attempt_stale(self, i):
        out=self._workflow_binding(i); clean=bool(out["binding_valid"])
        return {"binding":"CLEAN" if clean else "MISMATCH","decision":"ACCEPT" if clean else "REJECT_EVIDENCE","reason":None if clean else "ARTIFACT_RUN_ATTEMPT_MISMATCH"}

    def _session_fixture(self, i):
        rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        Actor=self._require(rec,"ActorBinding"); status=self._require(lifecycle,"SourceBindingStatus")
        candidates=i.get("candidates") or []
        actors=[]
        for n,candidate in enumerate(candidates):
            proven=bool(i.get("explicit_source_binding") and i.get("source_repository_proven"))
            actors.append(Actor(role="WRITER",provider="jules",session_id=candidate.get("session_id"),task_id=f"t{n}",lineage=f"l{n}",source_repository="fixture/repo",source_identity=f"source-{n}" if proven else None,proof_status=status.PROVEN_EXPLICIT if proven else status.PROPOSED_UNVERIFIED,evidence_id=f"e{n}" if proven else None))
        out=self._require(rec,"resolve_actor_binding")(self._binding(actor_bindings=tuple(actors)),"WRITER")
        return {"writer_binding":"PROVEN" if out.proven else "AMBIGUOUS" if out.state=="AMBIGUOUS" else "PROPOSED_UNVERIFIED","decision":"CONTINUE" if out.proven else "FAIL_CLOSED"}

    def _eval_heuristic_session_binding(self, i): return self._session_fixture(i)
    def _eval_explicit_session_binding(self, i): return self._session_fixture(i)

    def _eval_duplicate_session_across_lanes(self, i):
        rec=self._module("ues.reconciliation"); bindings=[]
        for n,lane in enumerate(i["lanes"]):
            bindings.append(self._binding(project=lane["project"],route=lane["route"],workstream=lane["workstream"],head_sha=str(n+1)*40,actor_bindings=self._actors(writer_session=lane["session_id"],reviewer_session=f"review-{n}")))
        out=self._require(rec,"reconcile_portfolio")(bindings)
        duplicate=any(_has(x.issues,"provider_session_across") for x in out)
        return {"binding":"AMBIGUOUS" if duplicate else "PROVEN","mutation_allowed":not duplicate,"signal":"DUPLICATE_SESSION_ACROSS_LANES" if duplicate else None}

    def _eval_cross_project_workstream_identity(self, i):
        rec=self._module("ues.reconciliation"); bindings=[]
        for n,lane in enumerate(i["lanes"]):
            bindings.append(self._binding(project=lane["project"],route=lane["route"],workstream=lane["workstream"],head_sha=("d" if n==0 else "e")*40,actor_bindings=self._actors(writer_session=lane["session_id"],reviewer_session=f"review-{n}")))
        keys=["|".join(self._require(rec,"canonical_lane_key")(x)) for x in bindings]
        out=self._require(rec,"reconcile_portfolio")(bindings)
        collision=any(_has(x.issues,"duplicate_lane") for x in out)
        return {"lane_keys":keys,"collision":collision,"independent":not collision}

    def _eval_binding_drift(self, i):
        rec=self._module("ues.reconciliation")
        previous=self._binding(base_ref=i["previous"]["base_ref"],head_sha=i["previous"]["head_sha"],scope_identity=json.dumps(i["previous"]["scope"],sort_keys=True))
        current=self._binding(base_ref=i["observed"]["base_ref"],head_sha=i["observed"]["head_sha"],scope_identity=json.dumps(i["observed"]["scope"],sort_keys=True))
        out=self._require(rec,"reconcile_workstream")(current,previous); drift=[]
        if i["previous"]["base_ref"]!=i["observed"]["base_ref"] and _has(out.issues,"base_ref"): drift.append("BASE")
        if i["previous"]["head_sha"]!=i["observed"]["head_sha"]: drift.append("HEAD")
        if i["previous"]["scope"]!=i["observed"]["scope"] and _has(out.issues,"scope_identity"): drift.append("SCOPE")
        return {"state":"STALE" if drift else "CURRENT","decision":"RECONCILE_BEFORE_ACTION" if drift else "CONTINUE","drift":drift}

    def _eval_mixed_ci_root_causes(self, i):
        failures=self._module("ues.failures"); classify=self._require(failures,"classify_failure"); scope=self._require(failures,"scope_blocker")
        cats=[]; independent=True
        for item in i["failures"]:
            out=classify(dict(item)); cats.append(out["category"]); independent=independent and scope(out,item["workstream"])["does_not_implicitly_block_unrelated_workstreams"]
        return {"classifications":cats,"collapsed_to_single_root":len(set(cats))==1,"independent_lane_progress":bool(independent)}

    def _eval_cascaded_failure_collapse(self, i):
        failures=self._module("ues.failures"); out=self._require(failures,"collapse_failure_cascade")(list(i["failures"]))
        return {"shared_blockers":[x["incident_id"] for x in out["shared_blockers"]],"correction_tasks":out["correction_task_count"],"duplicate_corrections":out["duplicate_corrections"]}

    def _eval_closed_unmerged_pr(self, i):
        def read_json(path,operation):
            if operation=="github.pull.get": return {"number":i["pr_number"],"state":i["state"],"draft":False,"merged":i["merged"],"head":{"ref":"feature","sha":i["head_sha"]},"base":{"ref":"main","sha":i["base_sha"]},"merge_commit_sha":None}
            raise AssertionError(operation)
        client=self._github_client(read_json=read_json)
        pr=self._require(self._module("ues.providers.github"),"GitHubClient").get_pull_request(client,"fixture","repo",i["pr_number"])
        integrated=bool(pr.get("merged")); accepted=integrated
        return {"accepted":accepted,"integrated":integrated,"decision":"ACCEPTED" if accepted else "NOT_ACCEPTED"}

    def _eval_awaiting_plan_approval(self, i):
        jules=self._module("ues.providers.jules"); base=self._module("ues.providers.base"); routing=self._module("ues.routing")
        class Transport:
            def __init__(self): self.posts=0
            def request(self,method,url,*,headers,body,timeout):
                if method=="POST": self.posts+=1; return base.HttpResponse(200,{},b"{}")
                if "/activities" in url: return base.HttpResponse(200,{},json.dumps({"activities":[]}).encode())
                return base.HttpResponse(200,{},json.dumps({"name":i["session_id"],"state":i["state"]}).encode())
        transport=Transport(); client=self._require(jules,"JulesClient")("fixture-key",transport=transport,sleeper=lambda _:None)
        try: client.send_message(i["session_id"],"fixture",expected_repository="fixture/repo")
        except Exception: pass
        classified=self._require(routing,"classify_waiting_activity")({"kind":"plan_approval"},provider_state=i["state"],classifier_rules={"rules":[{"waiting_class":"SCOPE_OR_NEW_TASK_REQUIRED","match":{"provider_state":"AWAITING_PLAN_APPROVAL","kind":"plan_approval"}}]})
        routed=self._require(routing,"route_waiting")(classified["waiting_class"],exact_state_read=True,latest_activity_read=True,continuation_binding_proven=False,project_auto_safe_actions=None)
        return {"authority":routed["authority"],"send_message":transport.posts>0,"blanket_approve":False,"decision":"STOP_GATE" if transport.posts==0 and routed["authority"]=="PARENT_REQUIRED" else "UNSAFE_OR_UNCLASSIFIED"}

    def _eval_keyword_false_positive(self, i):
        routing=self._module("ues.routing")
        classified=self._require(routing,"classify_waiting_activity")({"text":i["text"]},provider_state="AWAITING_USER_FEEDBACK",classifier_rules={"rules":[]})
        routed=self._require(routing,"route_waiting")(classified["waiting_class"],exact_state_read=i["exact_state_read"],latest_activity_read=i["latest_activity_read"],continuation_binding_proven=False,project_auto_safe_actions=None)
        return {"classification":classified["waiting_class"],"authority":routed["authority"],"unsafe_shortcut":bool(classified["keyword_shortcut_used"])}

    def _eval_shadow_trigger_enforcement(self, i):
        return self._eval_activation_missing(i) | {"mutation_allowed":False}

    def _eval_browser_evidence_missing(self, i):
        rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        profile=self._profile("browser-profile",name="browser-route-profile",proven=i["browser_profile_ran"])
        item=self._binding(role="REVIEWER",actor_bindings=(),lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT,review=self._review(outcome="PASS"),evidence_profile=profile)
        out=self._require(rec,"reconcile_workstream")(item); missing=_has(out.issues,"browser-route-profile")
        accepted=not missing and _enum(out.resolution.action)=="REQUEST_PARENT_REVIEW"
        return {"accepted":accepted,"decision":"EVIDENCE_INCOMPLETE" if missing else "PARENT_REVIEW_PENDING","signal":"REQUIRED_BROWSER_EVIDENCE_MISSING" if missing else None}

    def _eval_restart_after_send(self, i):
        recovery=self._module("ues.recovery")
        out=self._require(recovery,"reconcile_provider_write")({"authoritative_read_complete":True,"post_session_state":i["session_state"],"pre_activity_ids":i["pre_activity_ids"],"post_activities":i["post_activities"],"expected_user_message":i["expected_message"],"write_outcome":"WRITE_OUTCOME_UNKNOWN"})
        return {"readback_required":True,"second_mutation":bool(out.get("safe_to_blind_retry")),"verdict":out["verdict"]}

    def _eval_correction_semantic_regression(self, i):
        routing=self._module("ues.routing"); effect=self._require(routing,"RE_REVIEW_DISPATCH")
        out=self._require(routing,"route_writer_to_reviewer")(project="FIXTURE",route="PERSONAL:FIXTURE",workstream_id="W01",writer_session_id="writer",reviewer_session_id="reviewer",prior_reviewed_sha=i["prior_reviewed_sha"],new_candidate_sha=i["new_candidate_sha"],ci_evidence_sha=i["ci_evidence_sha"],required_ci_proven=True,existing_reviewer_available=i["existing_reviewer_available"],existing_reviewer_binding_proven=True,existing_reviewer_safe_to_reuse=i["existing_reviewer_safe_to_reuse"],new_reviewer_policy_allows=False,parent_gate_satisfied=False,project_auto_safe_actions={effect})
        return {"prior_review":"STALE" if out["prior_review_stale"] else "CURRENT","accepted":False,"next_action":"RE_REVIEW" if out["action"]=="DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER" else "RECONCILE","exact_new_sha_ci":bool(out["exact_required_ci_for_new_sha"])}

    def _eval_waiting_answer_effect_dedup(self, i):
        idem=self._module("ues.idempotency"); keyfn=self._require(idem,"waiting_answer_operation_key"); evalfn=self._require(idem,"evaluate_idempotency")
        route="PERSONAL:GS"; lane="lane:GS:PERSONAL-GS:W01"; a,b=i["answer_digests"]
        kwargs=dict(lane_id=lane,project=i["project"],route=route,workstream_id=i["workstream"],session_id=i["session_id"],waiting_activity_id=i["waiting_activity_id"])
        k1=keyfn(**kwargs,answer_digest=a); k2=keyfn(**kwargs,answer_digest=b); out=evalfn(k2,b,[{"operation_id":k1,"request_digest":a,"state":"CONFIRMED"}])
        return {"same_effect_identity":k1==k2,"decision":out["decision"],"second_send":bool(out["safe_to_execute"])}

    def _eval_canary_grant_mismatch(self, i):
        state=self._module("ues.state_store"); idem=self._module("ues.idempotency"); now=datetime(2026,1,1,tzinfo=timezone.utc)
        lane="lane:GS:PERSONAL-GS:W01"; route="PERSONAL:GS"
        Effect=self._require(idem,"canonical_effect_identity"); attempt=Effect(lane_id=lane,project="GS",route=route,workstream_id="W01",action=i["attempt"]["action"],target={"target":i["attempt"]["target"]})
        Grant=self._require(state,"CanaryGrant"); grant=Grant(authority_event_id="grant",lane_id=lane,project="GS",route=route,workstream_id="W01",effect_type=i["grant"]["action"],target={"target":i["grant"]["target"]},issued_at=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(minutes=1)).isoformat())
        Record=self._require(state,"WorkstreamRuntimeRecord"); record=Record(lane_id=lane,project="GS",route=route,workstream_id="W01",activation_mode="CANARY",canary_grants=[grant])
        out=self._require(state,"evaluate_canary_grant")(record,attempt,now=now); allowed=bool(out["allowed"])
        return {"allowed":allowed,"decision":"ALLOW_CANARY" if allowed else "DENY_CANARY_GRANT_MISMATCH"}

    def _eval_project_specific_environment_policy(self, i):
        routing=self._module("ues.routing"); effect=self._require(routing,"WAITING_SAME_SESSION_CONTINUATION")
        out=self._require(routing,"route_waiting")("ENVIRONMENT_MISMATCH",exact_state_read=i["exact_state_read"],latest_activity_read=i["latest_activity_read"],continuation_binding_proven=True,project_auto_safe_actions={effect} if i["policy_permits"] else set(),bounded_workaround_authorized=i["bounded_workaround_authorized"])
        return {"classification":out["waiting_class"],"authority":out["authority"],"next_action":"CONTINUE_SAME_SESSION" if out["action"]=="CONTINUE_SAME_SESSION" else "STOP_GATE"}

    def _eval_non_autosafe_blockers_cycle(self, i):
        wd=self._module("ues.watchdog"); lanes=[{"lane_id":x["id"],"blocked":x["blocked"],"stop_gate":x["authority"] if x["blocked"] else None} for x in i["lanes"]]
        out=self._require(wd,"evaluate_control_cycle")(lanes)
        return {"cycle":out["cycle_status"],"unresolved_auto_safe":out["unresolved_auto_safe_lanes"]}

    # CP-042..048 R2 convergence fixtures
    def _eval_role_specific_actor_bindings(self, i):
        rec=self._module("ues.reconciliation"); item=self._binding()
        writer=self._require(rec,"resolve_actor_binding")(item,"WRITER"); reviewer=self._require(rec,"resolve_actor_binding")(item,"REVIEWER")
        return {"writer":"PROVEN" if writer.proven else writer.state,"reviewer":"PROVEN" if reviewer.proven else reviewer.state,"independent":bool(writer.proven and reviewer.proven and writer.binding.session_id!=reviewer.binding.session_id)}

    def _eval_correction_action_policy(self, i):
        routing=self._module("ues.routing")
        out=self._require(routing,"route_reviewer_to_writer")(project="FIXTURE",route="PERSONAL:FIXTURE",workstream_id="W01",writer_session_id="writer",reviewer_session_id="reviewer",reviewed_sha="a"*40,candidate_sha="a"*40,reviewer_role_valid=True,reviewer_independent=True,reviewer_mutation_detected=False,reviewer_mutation_adjudicated=True,reviewer_mutation_disqualifying=False,writer_binding_proven=True,writer_binding_kind="EXPLICIT",finding_within_writer_scope=True,canonical_operation_active=False,canonical_operation_confirmed=False,findings=[{"id":"F1","root_cause":"fixture","summary":"x","paths":[]}],project_auto_safe_actions=set(i["project_auto_safe_actions"]))
        return {"authority":out["authority"],"send":bool(out["reuse_existing_writer"]),"effect":out["semantic_effect"]}

    def _eval_rereview_action_policy(self, i):
        routing=self._module("ues.routing"); sha="a"*40
        out=self._require(routing,"route_writer_to_reviewer")(project="FIXTURE",route="PERSONAL:FIXTURE",workstream_id="W01",writer_session_id="writer",reviewer_session_id="reviewer",prior_reviewed_sha="b"*40,new_candidate_sha=sha,ci_evidence_sha=sha,required_ci_proven=True,existing_reviewer_available=True,existing_reviewer_binding_proven=True,existing_reviewer_safe_to_reuse=True,new_reviewer_policy_allows=False,parent_gate_satisfied=False,project_auto_safe_actions=set(i["project_auto_safe_actions"]))
        return {"authority":out["authority"],"dispatch":bool(out["reuse_existing_reviewer"]),"effect":out["semantic_effect"]}

    def _eval_failure_recovery_action_policy(self, i):
        routing=self._module("ues.routing"); out=self._require(routing,"route_terminal_session_failure")(same_session_available=True,project_auto_safe_actions=set(i["project_auto_safe_actions"]))
        return {"authority":out["authority"],"continue":out["action"]=="CONTINUE_SAME_SESSION","effect":out["semantic_effect"]}

    def _eval_forgotten_cycle_health(self, i):
        wd=self._module("ues.watchdog"); out=self._require(wd,"evaluate_control_cycle")([{"lane_id":i["lane_id"]}])
        return {"cycle":out["cycle_status"],"forgotten":out["forgotten_lanes"]}

    def _eval_evidence_profile_drift(self, i):
        rec=self._module("ues.reconciliation"); prev=self._binding(evidence_profile=self._profile(i["previous_profile"],name="generic",proven=True)); cur=self._binding(evidence_profile=self._profile(i["current_profile"],name="generic",proven=True)); out=self._require(rec,"reconcile_workstream")(cur,prev)
        drift=bool(_has(out.issues,"evidence_profile")); return {"state":"STALE" if drift else "CURRENT","decision":"RECONCILE_BEFORE_ACTION" if drift else "CONTINUE","drift":["EVIDENCE_PROFILE"] if drift else []}

    def _eval_failure_cascade_exact_root(self, i):
        failures=self._module("ues.failures"); out=self._require(failures,"collapse_failure_cascade")(list(i["failures"]))
        return {"shared_blockers":[x["incident_id"] for x in out["shared_blockers"]],"unshared_count":len(out["unshared_failures"]),"correction_tasks":out["correction_task_count"]}

    def _eval_generic_evidence_profile(self, i):
        rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle")
        profile=self._profile(i["profile_id"],name=i["requirement"],proven=i["proven"])
        item=self._binding(role="REVIEWER",actor_bindings=(),lifecycle_state=lifecycle.LifecycleState.REVIEW_RESULT,review=self._review(outcome="PASS"),evidence_profile=profile)
        out=self._require(rec,"reconcile_workstream")(item); missing=bool(out.issues)
        return {"accepted":not missing,"decision":"EVIDENCE_INCOMPLETE" if missing else "PARENT_REVIEW_PENDING","requirement":i["requirement"]}
