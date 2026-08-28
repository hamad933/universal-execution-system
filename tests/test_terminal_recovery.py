from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from ues.lineage_registry import lineage_lane_id, session_fingerprint
from ues.state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord
from ues.terminal_recovery import (
    TERMINAL_RESULT_KEY,
    extract_terminal_candidate_with_legacy_recovery,
    persist_terminal_result,
    read_persisted_terminal_results,
    run_read_only_backfill,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
RP02_REPO = "hamad933/Enterprise-Operations-Control"
RP03_REPO = "hamad933/BOOKING-SERVICES"


@dataclass
class _Read:
    status: str
    version: int = 0
    record: WorkstreamRuntimeRecord | None = None
    reason: str | None = None


class FakeStore:
    def __init__(self, records=None):
        self.records = {}
        for lane_id, record in (records or {}).items():
            self.records[lane_id] = [1, WorkstreamRuntimeRecord.from_dict(record.to_dict())]
        self.cas_calls = 0
        self.discover_error = None
        self.cas_mode = None
        self.read_failures_after_cas = 0

    def discover_lane_ids(self):
        if self.discover_error:
            raise self.discover_error
        return list(self.records)

    def read_workstream(self, lane_id):
        if self.cas_calls and self.read_failures_after_cas > 0:
            self.read_failures_after_cas -= 1
            return _Read("UNAVAILABLE", reason="temporary readback outage")
        value = self.records.get(lane_id)
        if value is None:
            return _Read("MISSING")
        return _Read("OK", value[0], WorkstreamRuntimeRecord.from_dict(value[1].to_dict()))

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        self.cas_calls += 1
        current = self.records.get(lane_id)
        current_version = current[0] if current else 0
        if expected_version != current_version:
            raise StateVersionConflict("test conflict")
        if self.cas_mode == "conflict":
            raise StateVersionConflict("test conflict")
        new_version = current_version + 1
        self.records[lane_id] = [new_version, WorkstreamRuntimeRecord.from_dict(record.to_dict())]
        if self.cas_mode == "persist_then_unavailable":
            self.cas_mode = None
            raise StateUnavailable("post-CAS readback unavailable")
        if self.cas_mode == "unavailable_without_persist":
            self.records[lane_id] = current
            raise StateUnavailable("CAS transport unavailable")
        return _Read("OK", new_version, WorkstreamRuntimeRecord.from_dict(record.to_dict()))


class ConcurrentStore(FakeStore):
    def __init__(self, records, desired):
        super().__init__(records)
        self.desired = desired

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        self.cas_calls += 1
        current_version, current = self.records[lane_id]
        updated = WorkstreamRuntimeRecord.from_dict(current.to_dict())
        evidence = dict(updated.evidence_bindings or {})
        evidence[TERMINAL_RESULT_KEY] = dict(self.desired)
        updated.evidence_bindings = evidence
        self.records[lane_id] = [current_version + 1, updated]
        raise StateVersionConflict("concurrent exact persistence")


class FakeClient:
    def __init__(self, sessions, activities, repositories=None):
        self.sessions = list(sessions)
        self.activities = dict(activities)
        self.activity_calls = []
        self.source_calls = 0
        self.session_calls = 0
        repositories = repositories or {"sources/rp02": RP02_REPO}
        self.sources = [
            {"name": name, "repository": repo, "explicitRepositoryIdentity": True}
            for name, repo in repositories.items()
        ]
        self.provider_mutations = 0

    def list_sources(self, *, page_size=100):
        self.source_calls += 1
        return self.sources

    def list_sessions(self, *, page_size=100):
        self.session_calls += 1
        return self.sessions

    def list_activities(self, session, *, page_size=100):
        self.activity_calls.append(session)
        value = self.activities.get(session, [])
        if isinstance(value, Exception):
            raise value
        return value

    def create_session(self, *args, **kwargs):
        self.provider_mutations += 1
        raise AssertionError("terminal backfill must never create provider sessions")


def _record(
    *,
    project="RP02",
    route="RP02",
    workstream="S01",
    role="REVIEWER",
    generation=1,
    fp=None,
    repository=RP02_REPO,
    sha=SHA_A,
    pending=None,
):
    lane = lineage_lane_id(project, route, workstream, role)
    evidence = {
        "role": role,
        "workstream": workstream,
        "generation": generation,
        "session_fingerprint": fp,
        "current_candidate_sha": sha,
        "source_repository": repository,
        "binding_status": "PROVEN" if fp else "UNBOUND",
        "raw_session_id_persisted": False,
    }
    if pending is not None:
        evidence["pending_initial_lineage_transition"] = pending
    return lane, WorkstreamRuntimeRecord(
        lane_id=lane,
        project=project,
        route=route,
        workstream_id=f"LINEAGE::{workstream}::{role}",
        evidence_bindings=evidence,
        authority_provenance={"authority_event_id": "AUTH-1"},
    )


def _session(name, *, source="sources/rp02", branch="feature/review", title="RP02 S01 REVIEWER G1"):
    return {
        "name": name,
        "title": title,
        "normalizedState": "COMPLETED",
        "stateAuthoritative": True,
        "sourceIdentifier": source,
        "sourceStartingBranch": branch,
    }


def _handoff(*, workstream="S01", role="REVIEWER", reviewed_sha=SHA_A, findings=None):
    payload = {
        "role": role,
        "workstream": workstream,
        "status": "COMPLETE",
        "verdict": "FINDINGS" if findings else "PASS",
        "candidate_sha": None,
        "reviewed_sha": reviewed_sha,
        "context_state": "OK",
        "findings": findings or [],
    }
    return [{
        "name": "activities/private-id",
        "agentMessaged": {
            "agentMessage": "done\n<UES_HANDOFF_V1>\n" + json.dumps(payload) + "\n</UES_HANDOFF_V1>"
        },
    }]


class TerminalRecoveryTests(unittest.TestCase):
    def test_completed_valid_handoff_multiple_findings_persists_parent_consumable(self):
        name = "sessions/rp02-s01"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        client = FakeClient(
            [_session(name)],
            {name: _handoff(findings=[
                {"id": "F-1", "severity": "HIGH", "path": "app/a.py", "detail": "one"},
                {"id": "F-2", "severity": "MEDIUM", "path": "app/b.py", "detail": "two"},
            ])},
        )
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["result"], "TERMINAL_BACKFILL_COMPLETE")
        self.assertEqual(result["parent_consumable_result_count"], 1)
        self.assertEqual(client.activity_calls, [name])
        saved = store.records[lane][1].evidence_bindings[TERMINAL_RESULT_KEY]
        self.assertEqual(saved["finding_count"], 2)
        self.assertEqual(saved["result_state"], "PARENT_CONSUMABLE")
        self.assertFalse(saved["raw_session_id_persisted"])
        self.assertFalse(saved["raw_activity_content_persisted"])
        self.assertEqual(client.provider_mutations, 0)

    def test_malformed_and_plain_unstructured_never_infer_verdict(self):
        malformed = extract_terminal_candidate_with_legacy_recovery([
            {"agentMessaged": {"agentMessage": "<UES_HANDOFF_V1>{bad}</UES_HANDOFF_V1>"}}
        ])
        self.assertEqual(malformed["state"], "MALFORMED_STRUCTURED_HANDOFF")
        self.assertFalse(malformed["structured"])
        plain = extract_terminal_candidate_with_legacy_recovery([
            {"agentMessaged": {"agentMessage": "legacy prose says things but has no governed JSON"}}
        ])
        self.assertEqual(plain["state"], "COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION")
        self.assertFalse(plain["structured"])
        self.assertNotIn("verdict", plain)

    def test_markerless_json_legacy_output_is_safely_recovered(self):
        payload = {
            "role": "REVIEWER",
            "workstream": "S01",
            "status": "COMPLETE",
            "verdict": "PASS",
            "candidate_sha": None,
            "reviewed_sha": SHA_A,
            "context_state": "OK",
            "findings": [],
        }
        candidate = extract_terminal_candidate_with_legacy_recovery([
            {"agentMessaged": {"agentMessage": json.dumps(payload)}}
        ])
        self.assertTrue(candidate["structured"])
        self.assertEqual(candidate["legacy_recovery"], "MARKERLESS_JSON_HANDOFF_RECOVERED")
        self.assertEqual(candidate["verdict"], "PASS")

    def test_wrong_session_fingerprint_does_not_read_activity_or_bind_by_elimination(self):
        lane, record = _record(fp=session_fingerprint("sessions/other"))
        store = FakeStore({lane: record})
        name = "sessions/unbound"
        client = FakeClient([_session(name)], {name: _handoff()})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["unresolved_identity_count"], 1)
        self.assertEqual(client.activity_calls, [])
        self.assertEqual(result["outcomes"][0]["result_state"], "RESULT_IDENTITY_UNRESOLVED")

    def test_wrong_lineage_generation_and_repository_rejected_before_persistence(self):
        lane, record = _record(fp="fp", workstream="S01", generation=2)
        store = FakeStore({lane: record})
        result = {
            "session_fingerprint": "fp",
            "role": "REVIEWER",
            "logical_workstream": "S02",
            "generation": 1,
            "repository": RP02_REPO,
            "result_state": "PARENT_CONSUMABLE",
            "result_fingerprint": "r1",
        }
        lineage = {
            "lane_id": lane,
            "role": "REVIEWER",
            "workstream": "S01",
            "generation": 2,
        }
        outcome = persist_terminal_result(store, result=result, lineage=lineage)
        self.assertEqual(outcome["state"], "TERMINAL_RESULT_IDENTITY_NOT_EXACT")
        self.assertEqual(store.cas_calls, 0)
        result["logical_workstream"] = "S01"
        result["generation"] = 2
        result["repository"] = "foreign/repo"
        persisted = {**result, "lane_id": lane, "schema_version": "UES_TERMINAL_RESULT_V1"}
        record.evidence_bindings[TERMINAL_RESULT_KEY] = persisted
        store = FakeStore({lane: record})
        views = read_persisted_terminal_results(store, project="RP02", route="RP02", repository=RP02_REPO)
        self.assertEqual(views[0]["result_state"], "RESULT_IDENTITY_UNRESOLVED")

    def test_wrong_reviewed_sha_is_stale_and_not_parent_consumable(self):
        name = "sessions/rp02-stale"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        client = FakeClient([_session(name)], {name: _handoff(reviewed_sha=SHA_B)})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["parent_consumable_result_count"], 0)
        self.assertEqual(result["outcomes"][0]["result_state"], "REVIEWED_SHA_MISMATCH")

    def test_stored_result_becomes_stale_after_candidate_movement_without_provider_reread(self):
        name = "sessions/rp02-moved"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        record.evidence_bindings[TERMINAL_RESULT_KEY] = {
            "schema_version": "UES_TERMINAL_RESULT_V1",
            "project": "RP02",
            "route": "RP02",
            "logical_workstream": "S01",
            "role": "REVIEWER",
            "generation": 1,
            "session_fingerprint": fp,
            "repository": RP02_REPO,
            "reviewed_sha": SHA_A,
            "result_state": "PARENT_CONSUMABLE",
            "result_fingerprint": "persisted",
        }
        record.evidence_bindings["current_candidate_sha"] = SHA_B
        store = FakeStore({lane: record})
        views = read_persisted_terminal_results(store, project="RP02", route="RP02", repository=RP02_REPO)
        self.assertEqual(views[0]["result_state"], "REVIEWED_SHA_MISMATCH")
        self.assertEqual(views[0]["freshness_status"], "STALE_AFTER_CANDIDATE_MOVEMENT")

    def test_statestore_unavailable_before_provider_read_performs_zero_provider_reads(self):
        store = FakeStore()
        store.discover_error = StateUnavailable("HTTP 403")
        client = FakeClient([], {})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["result"], "TERMINAL_BACKFILL_STATESTORE_UNAVAILABLE_BEFORE_PROVIDER_READ")
        self.assertFalse(result["provider_read_started"])
        self.assertEqual(client.source_calls, 0)
        self.assertEqual(client.session_calls, 0)
        self.assertEqual(client.provider_mutations, 0)

    def test_provider_read_succeeds_then_cas_persists_but_readback_reconciles(self):
        name = "sessions/rp02-reconcile"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        store.cas_mode = "persist_then_unavailable"
        client = FakeClient([_session(name)], {name: _handoff()})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["parent_consumable_result_count"], 1)
        self.assertIn(result["outcomes"][0]["persistence_state"], {
            "TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
            "TERMINAL_RESULT_PERSISTED",
        })
        self.assertEqual(store.cas_calls, 1)

    def test_provider_read_succeeds_then_persistence_failure_is_partial_not_reexecution(self):
        name = "sessions/rp02-persist-fail"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        store.cas_mode = "unavailable_without_persist"
        client = FakeClient([_session(name)], {name: _handoff()})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["result"], "TERMINAL_BACKFILL_PARTIAL_STATESTORE_RECOVERY_REQUIRED")
        self.assertTrue(result["provider_read_complete"])
        self.assertFalse(result["state_persistence_complete"])
        self.assertEqual(client.activity_calls, [name])
        self.assertEqual(client.provider_mutations, 0)

    def test_persistence_success_with_temporary_final_readback_failure_never_repeats_cas(self):
        name = "sessions/rp02-readback"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        store.read_failures_after_cas = 1
        result = {
            "project": "RP02",
            "route": "RP02",
            "logical_workstream": "S01",
            "role": "REVIEWER",
            "generation": 1,
            "session_fingerprint": fp,
            "repository": RP02_REPO,
            "reviewed_sha": SHA_A,
            "result_state": "PARENT_CONSUMABLE",
            "result_fingerprint": "result-x",
        }
        lineage = {"lane_id": lane, "role": "REVIEWER", "workstream": "S01", "generation": 1}
        outcome = persist_terminal_result(store, result=result, lineage=lineage)
        self.assertEqual(outcome["state"], "TERMINAL_RESULT_PERSISTED_READBACK_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(store.cas_calls, 1)

    def test_duplicate_backfill_and_process_restart_skip_expensive_activity_reread(self):
        name = "sessions/rp02-idempotent"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        first_client = FakeClient([_session(name)], {name: _handoff()})
        first = run_read_only_backfill(["RP02"], store=store, client=first_client)
        self.assertEqual(first["parent_consumable_result_count"], 1)
        second_client = FakeClient([_session(name)], {name: _handoff()})
        second = run_read_only_backfill(["RP02"], store=store, client=second_client)
        self.assertEqual(second["parent_consumable_result_count"], 1)
        self.assertEqual(second_client.activity_calls, [])
        self.assertEqual(store.cas_calls, 1)

    def test_exact_pending_transition_marker_reconciles_legacy_identity_without_provider_mutation(self):
        name = "sessions/rp02-pending"
        fp = session_fingerprint(name)
        marker = "abc123def456"
        pending = {
            "transition_key": marker + "7890",
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "current_generation": 0,
            "next_generation": 1,
            "source_repository": RP02_REPO,
            "source_name": "sources/rp02",
            "starting_branch": "feature/review",
            "candidate_sha": SHA_A,
            "task_spec_digest": "d" * 64,
            "provider_title_marker": marker,
            "safe_to_blind_retry": False,
        }
        lane, record = _record(generation=0, fp=None, pending=pending)
        store = FakeStore({lane: record})
        client = FakeClient(
            [_session(name, title=f"RP02 S01 REVIEWER G1 [{marker}]")],
            {name: _handoff()},
        )
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["identity_reconciled_count"], 1)
        self.assertEqual(result["parent_consumable_result_count"], 1)
        self.assertEqual(store.records[lane][1].evidence_bindings["session_fingerprint"], fp)
        self.assertEqual(client.provider_mutations, 0)

    def test_multiple_marker_matches_remain_identity_unresolved_and_isolated(self):
        unresolved_name = "sessions/rp02-unresolved"
        good_name = "sessions/rp02-good"
        good_fp = session_fingerprint(good_name)
        marker = "same-marker1"
        pending = {
            "transition_key": marker + "-transition",
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "next_generation": 1,
            "source_repository": RP02_REPO,
            "source_name": "sources/rp02",
            "starting_branch": "feature/review",
            "candidate_sha": SHA_A,
            "task_spec_digest": "x",
            "provider_title_marker": marker,
        }
        lane1, rec1 = _record(workstream="S01", generation=0, fp=None, pending=pending)
        lane2, rec2 = _record(workstream="S02", generation=0, fp=None, pending=pending)
        lane3, rec3 = _record(workstream="S03", fp=good_fp)
        store = FakeStore({lane1: rec1, lane2: rec2, lane3: rec3})
        client = FakeClient(
            [
                _session(unresolved_name, title=f"ambiguous [{marker}]"),
                _session(good_name, title="good exact"),
            ],
            {unresolved_name: _handoff(), good_name: _handoff(workstream="S03")},
        )
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertGreaterEqual(result["unresolved_identity_count"], 1)
        self.assertEqual(result["parent_consumable_result_count"], 1)
        self.assertEqual(client.activity_calls, [good_name])

    def test_concurrent_exact_persistence_is_reconciled_without_second_cas(self):
        name = "sessions/rp02-concurrent"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        result = {
            "project": "RP02",
            "route": "RP02",
            "logical_workstream": "S01",
            "role": "REVIEWER",
            "generation": 1,
            "session_fingerprint": fp,
            "repository": RP02_REPO,
            "reviewed_sha": SHA_A,
            "result_state": "PARENT_CONSUMABLE",
            "result_fingerprint": "concurrent-result",
        }
        desired = {**result, "schema_version": "UES_TERMINAL_RESULT_V1", "lane_id": lane}
        store = ConcurrentStore({lane: record}, desired)
        lineage = {"lane_id": lane, "role": "REVIEWER", "workstream": "S01", "generation": 1}
        outcome = persist_terminal_result(store, result=result, lineage=lineage)
        self.assertEqual(outcome["state"], "TERMINAL_RESULT_CONCURRENTLY_PERSISTED")
        self.assertEqual(store.cas_calls, 1)

    def test_secret_shapes_are_redacted_and_raw_provider_identity_never_persisted(self):
        name = "sessions/rp02-secret"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = FakeStore({lane: record})
        client = FakeClient([_session(name)], {name: _handoff(findings=[{
            "id": "F-SECRET",
            "severity": "HIGH",
            "path": "app/a.py",
            "detail": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "recommended_action": "use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        }])})
        result = run_read_only_backfill(["RP02"], store=store, client=client)
        self.assertEqual(result["parent_consumable_result_count"], 1)
        rendered = json.dumps(store.records[lane][1].to_dict())
        self.assertNotIn("Bearer abcdef", rendered)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ", rendered)
        self.assertNotIn(name, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_multi_project_backfill_has_no_cross_project_binding_leakage(self):
        rp02_name = "sessions/rp02-multi"
        rp03_name = "sessions/rp03-multi"
        rp02_fp = session_fingerprint(rp02_name)
        rp03_fp = session_fingerprint(rp03_name)
        lane2, rec2 = _record(fp=rp02_fp)
        lane3, rec3 = _record(
            project="RP03",
            route="RP03",
            workstream="S09",
            fp=rp03_fp,
            repository=RP03_REPO,
        )
        store = FakeStore({lane2: rec2, lane3: rec3})
        client = FakeClient(
            [
                _session(rp02_name),
                _session(rp03_name, source="sources/rp03", title="RP03 S09 ASSURANCE"),
            ],
            {
                rp02_name: _handoff(),
                rp03_name: _handoff(workstream="S09"),
            },
            repositories={"sources/rp02": RP02_REPO, "sources/rp03": RP03_REPO},
        )
        result = run_read_only_backfill(["RP02", "RP03"], store=store, client=client)
        self.assertEqual(result["parent_consumable_result_count"], 2)
        self.assertEqual(result["projects"]["RP02"]["parent_consumable_result_count"], 1)
        self.assertEqual(result["projects"]["RP03"]["parent_consumable_result_count"], 1)
        self.assertEqual(client.provider_mutations, 0)


if __name__ == "__main__":
    unittest.main()
