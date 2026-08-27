from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping

from .evidence_supplement_runtime import _resolve_unique_source
from .lineage_registry import lineage_lane_id, session_fingerprint
from .live_runtime import build_live_state_store
from .providers.jules import JulesClient
from .terminal_recovery import extract_terminal_candidate_with_legacy_recovery, persist_terminal_result
from .terminal_results import _bound_result

_ALLOWED_PROJECTS = frozenset({"RP01", "RP02", "RP03", "RP04"})
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROLE = "ASSURANCE"


def _lane(store: Any, project: str, workstream: str) -> tuple[str, Any, Mapping[str, Any]]:
    lane_id = lineage_lane_id(project, project, workstream, _ROLE)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise ValueError("exact evidence-supplement lane is not durably bound")
    evidence = read.record.evidence_bindings or {}
    if str(evidence.get("workstream") or "") != workstream or str(evidence.get("role") or "").upper() != _ROLE:
        raise ValueError("evidence-supplement lane identity mismatch")
    if int(evidence.get("generation") or 0) <= 0 or not str(evidence.get("session_fingerprint") or "").strip():
        raise ValueError("evidence-supplement generation/session binding is incomplete")
    repository_alias = str(evidence.get("source_repository") or "").strip()
    if not repository_alias.startswith("sha256:"):
        raise ValueError("evidence-supplement transport repository fingerprint missing")
    return lane_id, read.record, evidence


def run(project: str, workstream: str, *, store: Any | None = None, client: Any | None = None) -> dict[str, Any]:
    project_id = str(project or "").strip().upper()
    target = str(workstream or "").strip()
    if project_id not in _ALLOWED_PROJECTS or not _WORKSTREAM.fullmatch(target):
        raise ValueError("exact RP project/workstream required")
    effective_store = store or build_live_state_store()
    effective_client = client
    if effective_client is None:
        import os
        key = str(os.environ.get("JULES_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("JULES_API_KEY is required")
        effective_client = JulesClient(key)

    lane_id, _record, evidence = _lane(effective_store, project_id, target)
    expected_fp = str(evidence.get("session_fingerprint") or "").strip().lower()
    generation = int(evidence.get("generation") or 0)
    repository_alias = str(evidence.get("source_repository") or "").strip()
    candidate_sha = str(evidence.get("current_candidate_sha") or "").strip() or None
    starting_branch = str(evidence.get("provider_starting_branch") or "").strip()

    resolved = _resolve_unique_source(effective_client, repository_alias)
    if resolved is None:
        return _receipt(project_id, target, expected_fp, generation, candidate_sha,
                        result="SUPPLEMENT_PRIVATE_SOURCE_UNRESOLVED")
    actual_source_name, _actual_repository = resolved

    matches = []
    for session in effective_client.list_sessions(page_size=100):
        name = str(session.get("name") or "").strip()
        if not name or session_fingerprint(name) != expected_fp:
            continue
        if str(session.get("sourceIdentifier") or "").strip().strip("/") != actual_source_name.strip().strip("/"):
            continue
        observed_branch = str(session.get("sourceStartingBranch") or "").strip()
        if starting_branch and observed_branch and observed_branch != starting_branch:
            continue
        matches.append(session)
    if len(matches) != 1:
        result = "SUPPLEMENT_SESSION_NOT_VISIBLE" if not matches else "SUPPLEMENT_SESSION_IDENTITY_AMBIGUOUS"
        return _receipt(project_id, target, expected_fp, generation, candidate_sha, result=result)

    session = matches[0]
    state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
    if state != "COMPLETED":
        receipt = _receipt(project_id, target, expected_fp, generation, candidate_sha,
                           result="SUPPLEMENT_SESSION_NONTERMINAL")
        receipt["provider_state"] = state
        return receipt

    activities = effective_client.list_activities(str(session.get("name") or ""), page_size=100)
    candidate = extract_terminal_candidate_with_legacy_recovery(activities)
    lineage = {
        "lane_id": lane_id,
        "role": _ROLE,
        "workstream": target,
        "generation": generation,
        "current_candidate_sha": candidate_sha,
    }
    public_session = {
        "session_fingerprint": expected_fp,
        "source_repository": repository_alias,
        "source_binding_proven": True,
    }
    if candidate.get("structured") is True:
        result = _bound_result(
            project=project_id,
            route=project_id,
            repository=repository_alias,
            session=public_session,
            candidate=candidate,
            lineage=lineage,
        )
    else:
        result = {
            "schema_version": "1.0", "project": project_id, "route": project_id,
            "logical_workstream": target, "role": _ROLE, "generation": generation,
            "session_fingerprint": expected_fp, "repository": repository_alias,
            "status": "COMPLETE", "verdict": None, "finding_count": None, "findings": [],
            "result_state": str(candidate.get("state") or "COMPLETED_OUTPUT_UNSTRUCTURED"),
            "freshness_status": "UNADJUDICABLE", "parent_action_required": True,
            "raw_activity_content_persisted": False, "raw_session_id_persisted": False,
        }
        from hashlib import sha256
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        result["result_fingerprint"] = sha256(raw.encode("utf-8")).hexdigest()

    persistence = persist_terminal_result(effective_store, result=result, lineage=lineage)
    return {
        "schema_version": "UES_EVIDENCE_SUPPLEMENT_TERMINAL_V1",
        "project": project_id,
        "logical_workstream": target,
        "generation": generation,
        "session_fingerprint": expected_fp,
        "provider_state": "COMPLETED",
        "candidate_sha": candidate_sha,
        "terminal_result": result,
        "persistence": persistence,
        "private_source_identity_persisted": False,
        "provider_mutation_performed": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
    }


def _receipt(project: str, workstream: str, fp: str, generation: int, candidate_sha: str | None, *, result: str) -> dict[str, Any]:
    return {
        "schema_version": "UES_EVIDENCE_SUPPLEMENT_TERMINAL_V1",
        "project": project, "logical_workstream": workstream, "generation": generation,
        "session_fingerprint": fp, "candidate_sha": candidate_sha, "result": result,
        "private_source_identity_persisted": False, "provider_mutation_performed": False,
        "external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume one exact private evidence-supplement terminal result")
    parser.add_argument("project", choices=sorted(_ALLOWED_PROJECTS))
    parser.add_argument("workstream")
    args = parser.parse_args(argv)
    result = run(args.project, args.workstream)
    print(json.dumps(result, sort_keys=True))
    terminal = result.get("provider_state") == "COMPLETED"
    return 0 if terminal else 3


if __name__ == "__main__":
    raise SystemExit(main())
