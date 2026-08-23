"""Thin integration adapters from replay fixtures to composed A-D production APIs.

No semantic fallback exists here. Missing production capabilities are hard integration
failures; the independent expectation oracle is deliberately outside this module.
"""
from __future__ import annotations

import importlib
import inspect
import json
import tempfile
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class IntegrationBindingUnavailable(AssertionError):
    pass


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _contains_token(value: Any, token: str) -> bool:
    needle = token.upper()
    if isinstance(value, str):
        return needle in value.upper()
    if isinstance(value, Mapping):
        return any(_contains_token(k, token) or _contains_token(v, token) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_token(v, token) for v in value)
    return False


def _construct(cls: Any, values: Mapping[str, Any]) -> Any:
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError) as exc:
        raise IntegrationBindingUnavailable(f"cannot inspect production type {cls!r}: {exc}") from exc
    kwargs = {k: v for k, v in values.items() if k in sig.parameters}
    missing = [
        name for name, p in sig.parameters.items()
        if name != "self" and p.default is inspect.Parameter.empty and p.kind in {p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY} and name not in kwargs
    ]
    if missing:
        raise IntegrationBindingUnavailable(f"production type {cls.__module__}.{cls.__name__} requires unmapped fields: {missing}")
    try:
        return cls(**kwargs)
    except Exception as exc:
        raise IntegrationBindingUnavailable(f"cannot construct production type {cls.__module__}.{cls.__name__}: {exc}") from exc



__all__ = ["inspect","json","tempfile","datetime","timezone","Path","Mapping","IntegrationBindingUnavailable","_enum_value","_contains_token","_construct"]

class AdapterBase:
    """Shared lazy production bindings; no reference-oracle dependency."""

    def _module(self, name: str):
            try:
                return importlib.import_module(name)
            except Exception as exc:
                raise IntegrationBindingUnavailable(f"production module unavailable: {name}: {exc}") from exc

    def _callable(self, module: Any, name: str):
            value = getattr(module, name, None)
            if not callable(value):
                raise IntegrationBindingUnavailable(f"production callable unavailable: {module.__name__}.{name}")
            return value

    def _semantic_callable(self, module: Any, semantic: str, names: tuple[str, ...]):
            for name in names:
                value = getattr(module, name, None)
                if callable(value):
                    return value
            raise IntegrationBindingUnavailable(
                f"production semantic binding unavailable: {semantic}; checked {module.__name__}: {', '.join(names)}"
            )

    def evaluate(self, case: Any) -> dict[str, Any]:
            method = getattr(self, f"_eval_{case.kind}", None)
            if not callable(method):
                raise IntegrationBindingUnavailable(f"no production adapter for replay kind {case.kind}")
            return method(case.inputs)

    def _base_binding(self, **overrides: Any):
            rec = self._module("ues.reconciliation")
            lifecycle = self._module("ues.lifecycle")
            values = {
                "project": "FIXTURE",
                "route": "PERSONAL:FIXTURE",
                "workstream": "W01",
                "workstream_id": "W01",
                "role": "WRITER",
                "repo": "fixture/repo",
                "repository": "fixture/repo",
                "branch": "feature/fixture",
                "lifecycle_state": getattr(lifecycle.LifecycleState, "PARENT_REVIEW_PENDING"),
                "baseline_sha": "0" * 40,
                "base_ref": "main",
                "task_budget_class": "FIXTURE",
                "last_activity_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "head_sha": "1" * 40,
                "writer_lineage": "writer-lineage-fixture",
                "reviewer_lineage": "reviewer-lineage-fixture",
                "pr_number": 7,
                "jules_session_id": "sessions/fixture-writer",
                "session_id": "sessions/fixture-writer",
            }
            values.update(overrides)
            return _construct(rec.WorkstreamBinding, values)

    def _review_binding(self, **overrides: Any):
            rec = self._module("ues.reconciliation")
            lifecycle = self._module("ues.lifecycle")
            values = {"review_id":"review-fixture","reviewed_sha":"1"*40,"reviewer_lineage":"reviewer-lineage-fixture","outcome":getattr(lifecycle.ReviewOutcome,"PASS"),"stale":False}
            values.update(overrides)
            return _construct(rec.ReviewBinding, values)

    def _ci_binding(self, **overrides: Any):
            rec = self._module("ues.reconciliation")
            lifecycle = self._module("ues.lifecycle")
            values = {"run_id":"100","job_id":"200","artifact_id":"300","candidate_sha":"1"*40,"outcome":getattr(lifecycle.CIOutcome,"PASS"),"run_attempt":1,"artifact_digest":"fixture-digest","producer_job":"core-ci","required_checks":["Core CI / test"]}
            values.update(overrides)
            return _construct(rec.CIBinding, values)

    def _github_synthetic_client(self, *, read_json=None, paginate=None):
            gh = self._module("ues.providers.github")
            base = self._module("ues.providers.base")
            parent = gh.GitHubClient
            class Synthetic(parent):
                def __init__(self):
                    pass
                def _read_json(self, path, *, operation, extra_headers=None):
                    if read_json is None:
                        raise AssertionError(f"unexpected synthetic GitHub read: {operation}")
                    return read_json(path, operation)
                def _paginate(self, path, *, item_key, operation, extra_headers=None):
                    if paginate is None:
                        raise AssertionError(f"unexpected synthetic GitHub pagination: {operation}")
                    return paginate(path, operation)
            return Synthetic(), base

    def _session_binding(self, i):
            rec=self._module("ues.reconciliation")
            fn=self._semantic_callable(rec,"explicit/source-backed provider session binding",("resolve_session_binding","reconcile_session_binding","evaluate_session_binding"))
            try:
                out=fn(dict(i))
            except TypeError as exc:
                raise IntegrationBindingUnavailable(f"session binding API cannot consume fixture snapshot: {exc}") from exc
            if not isinstance(out, Mapping):
                raise IntegrationBindingUnavailable("session binding API did not return a mapping")
            status=out.get("binding") or out.get("status") or out.get("writer_binding")
            decision=out.get("decision") or ("CONTINUE" if status=="PROVEN" else "FAIL_CLOSED")
            return {"writer_binding":_enum_value(status),"decision":_enum_value(decision)}

    def _workflow_binding(self, i):
            run_id=int(i.get("run_id",i.get("expected_run_id"))); attempt=int(i.get("candidate_attempt",i.get("expected_attempt"))); art_attempt=int(i.get("artifact_attempt")); sha=i.get("candidate_sha","d"*40); artifact_id=int(i.get("artifact_id",4001)); producer=i.get("producer_job","fixture-job")
            def read_json(path,operation):
                if operation=="github.workflow_run.get": return {"id":run_id,"run_attempt":attempt,"head_sha":sha}
                raise AssertionError(operation)
            def paginate(path,operation):
                if operation=="github.workflow_jobs.list": return [{"id":5001,"run_id":run_id,"run_attempt":attempt,"head_sha":sha,"name":producer}]
                if operation=="github.workflow_artifacts.list": return [{"id":artifact_id,"name":"fixture-artifact","digest":i.get("artifact_digest","fixture-digest"),"run_attempt":art_attempt,"producer_job":producer,"workflow_run":{"id":run_id,"head_sha":sha,"run_attempt":art_attempt}}]
                raise AssertionError(operation)
            client,_=self._github_synthetic_client(read_json=read_json,paginate=paginate)
            fn=client.get_workflow_binding
            kwargs={"expected_sha":sha,"expected_run_attempt":attempt}
            out=fn("fixture","repo",run_id,**kwargs)
            return out
