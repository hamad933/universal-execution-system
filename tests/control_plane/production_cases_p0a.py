from __future__ import annotations

from tests.control_plane.production_adapter_base import *


class P0CasesAMixin:

    def _eval_required_ci_missing(self, i):
            def paginate(path,operation):
                if operation=="github.statuses.list": return list(i["statuses"])
                if operation=="github.checks.list": return list(i["check_runs"])
                raise AssertionError(operation)
            client,_=self._github_synthetic_client(paginate=paginate)
            fn=client.get_ci_evidence
            sig=inspect.signature(fn); kwargs={}
            for name in ("required_checks","required_check_names","required_ci"):
                if name in sig.parameters: kwargs[name]=list(i["required_checks"])
            out=fn("fixture","repo",i["candidate_sha"],**kwargs)
            missing=_contains_token(out,"REQUIRED_CI_MISSING")
            return {"decision":"NOT_A_PASS" if not out.get("pass_authorized") else "PASS","signal":"REQUIRED_CI_MISSING" if missing else None,"pass_authorized":bool(out.get("pass_authorized"))}

    def _eval_artifact_attempt_stale(self, i):
            out=self._workflow_binding(i); mismatch=not bool(out.get("binding_valid")) or bool(out.get("artifact_mismatches"))
            token=_contains_token(out,"ARTIFACT_RUN_ATTEMPT_MISMATCH")
            return {"binding":"MISMATCH" if mismatch else "CLEAN","decision":"REJECT_EVIDENCE" if mismatch else "ACCEPT","reason":"ARTIFACT_RUN_ATTEMPT_MISMATCH" if token else None}

    def _eval_heuristic_session_binding(self, i): return self._session_binding(i)

    def _eval_explicit_session_binding(self, i): return self._session_binding(i)

    def _eval_duplicate_session_across_lanes(self, i):
            rec=self._module("ues.reconciliation"); fn=self._callable(rec,"reconcile_portfolio")
            bindings=[]
            for n,lane in enumerate(i["lanes"],1):
                bindings.append(self._base_binding(project=lane["project"],route=lane["route"],workstream=lane["workstream"],workstream_id=lane["workstream"],jules_session_id=lane["session_id"],session_id=lane["session_id"],head_sha=str(n)*40))
            out=fn(bindings)
            duplicate=any(_contains_token(getattr(x,"issues",()),"DUPLICATE_SESSION") or _contains_token(getattr(x,"resolution",None),"DUPLICATE_SESSION") for x in out)
            return {"binding":"AMBIGUOUS" if duplicate else "PROVEN","mutation_allowed":not duplicate,"signal":"DUPLICATE_SESSION_ACROSS_LANES" if duplicate else None}

    def _eval_cross_project_workstream_identity(self, i):
            rec=self._module("ues.reconciliation"); fn=self._callable(rec,"reconcile_portfolio")
            bindings=[self._base_binding(project=x["project"],route=x["route"],workstream=x["workstream"],workstream_id=x["workstream"],jules_session_id=x["session_id"],session_id=x["session_id"],head_sha=("d" if x["project"]=="GS" else "e")*40) for x in i["lanes"]]
            out=fn(bindings); collision=any(_contains_token(getattr(x,"issues",()),"DUPLICATE_WORKSTREAM") or _contains_token(getattr(x,"resolution",None),"AMBIGUOUS_WORKSTREAM") for x in out)
            keys=[f"{getattr(b,'project',None)}|{getattr(b,'route',None)}|{getattr(b,'workstream',getattr(b,'workstream_id',None))}" for b in bindings]
            return {"lane_keys":keys,"collision":collision,"independent":not collision}

    def _eval_binding_drift(self, i):
            rec=self._module("ues.reconciliation"); lifecycle=self._module("ues.lifecycle"); fn=self._callable(rec,"reconcile_workstream")
            def mk(which):
                data=i[which]
                extra={"base_ref":data["base_ref"],"head_sha":data["head_sha"],"lifecycle_state":getattr(lifecycle.LifecycleState,"REVIEW_RESULT"),"review":self._review_binding(reviewed_sha=i["previous"]["head_sha"])}
                for key in ("scope","allowed_scope","allowed_paths","write_scope"):
                    extra[key]=data["scope"]
                return self._base_binding(**extra)
            out=fn(mk("observed"),mk("previous")); drift=[]
            if _contains_token(out,"BASE") and _contains_token(out,"MISMATCH"): drift.append("BASE")
            if getattr(out,"candidate_sha_moved",False) or _contains_token(out,"HEAD_MOVED"): drift.append("HEAD")
            if _contains_token(out,"SCOPE") and (_contains_token(out,"MISMATCH") or _contains_token(out,"DRIFT")): drift.append("SCOPE")
            return {"state":"STALE" if drift else "CURRENT","decision":"RECONCILE_BEFORE_ACTION" if drift else "CONTINUE","drift":drift}

    def _eval_mixed_ci_root_causes(self, i):
            f=self._module("ues.failures"); classify=self._callable(f,"classify_failure"); scope=self._callable(f,"scope_blocker")
            cats=[]; independent=True
            for item in i["failures"]:
                out=classify(dict(item)); cats.append(out.get("category")); block=scope(out,item["workstream"]); independent=independent and bool(block.get("does_not_implicitly_block_unrelated_workstreams"))
            return {"classifications":cats,"collapsed_to_single_root":len(set(cats))==1,"independent_lane_progress":independent}

    def _eval_cascaded_failure_collapse(self, i):
            f=self._module("ues.failures")
            fn=self._semantic_callable(f,"cascaded failure root-cause collapse",("collapse_cascaded_failures","collapse_failure_cascade","group_shared_failure_blockers"))
            out=fn(list(i["failures"]))
            if not isinstance(out,Mapping): raise IntegrationBindingUnavailable("cascaded failure collapse API did not return mapping")
            blockers=out.get("shared_blockers") or out.get("blockers") or []
            ids=[x.get("incident_id") if isinstance(x,Mapping) else x for x in blockers]
            return {"shared_blockers":ids,"correction_tasks":int(out.get("correction_tasks",out.get("correction_task_count",0))),"duplicate_corrections":bool(out.get("duplicate_corrections"))}

    def _eval_closed_unmerged_pr(self, i):
            def read_json(path,operation):
                if operation=="github.pull.get": return {"number":i["pr_number"],"state":i["state"],"draft":False,"merged":i["merged"],"head":{"ref":"feature/fixture","sha":i["head_sha"]},"base":{"ref":"main","sha":i["base_sha"]},"merge_commit_sha":None}
                raise AssertionError(operation)
            client,_=self._github_synthetic_client(read_json=read_json)
            pr=client.get_pull_request("fixture","repo",i["pr_number"])
            integrated=bool(pr.get("merged")); accepted=integrated and pr.get("state")=="closed"
            return {"accepted":accepted,"integrated":integrated,"decision":"ACCEPTED" if accepted else "NOT_ACCEPTED"}
