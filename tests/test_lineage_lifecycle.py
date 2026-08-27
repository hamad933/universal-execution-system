from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.lineage_registry import continuation_disposition, match_lineage_session, session_fingerprint, upsert_lineage_observation
from ues.recovery_catalog import plan_recovery
from ues.state_store import DeterministicFileStateStore
from ues.structured_handoff import END_MARKER, START_MARKER, find_latest_structured_handoff, find_latest_structured_handoff_runtime


class LineageRegistryTests(unittest.TestCase):
    def test_reuse_and_terminal_dispositions(self) -> None:
        self.assertEqual(continuation_disposition("AWAITING_USER_FEEDBACK"), "REUSE_SAME_SESSION")
        self.assertEqual(continuation_disposition("IN_PROGRESS"), "REUSE_SAME_SESSION")
        self.assertEqual(continuation_disposition("COMPLETED"), "TERMINAL_REPLACE_ONLY_IF_MORE_WORK_REQUIRED")
        self.assertEqual(continuation_disposition("FAILED"), "TERMINAL_REPLACE_ONLY_IF_MORE_WORK_REQUIRED")

    def test_exact_fingerprint_uses_repository_without_conflating_pr_head_branch(self) -> None:
        expected = session_fingerprint("sessions/exact")
        sessions = [{"name":"sessions/exact","normalizedState":"COMPLETED","_source_repository":"owner/repo","_session_fingerprint":expected,"sourceStartingBranch":"provider-base"}]
        result = match_lineage_session(sessions,{"known_session_fingerprints":[expected],"pr_head_branch":"work/output"},repository="owner/repo")
        self.assertEqual(result["status"], "PROVEN")
        self.assertEqual(result["provider_state"], "COMPLETED")

    def test_provider_starting_branch_drift_is_diagnostic_after_exact_identity(self) -> None:
        expected = session_fingerprint("sessions/exact")
        sessions = [{"name":"sessions/exact","normalizedState":"COMPLETED","_source_repository":"owner/repo","_session_fingerprint":expected,"sourceStartingBranch":"provider-base"}]
        result = match_lineage_session(sessions,{"known_session_fingerprints":[expected],"provider_starting_branch":"other-base"},repository="owner/repo")
        self.assertEqual(result["status"], "PROVEN")
        self.assertEqual(result["reason"], "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_DRIFT")
        self.assertTrue(result["provider_starting_branch_metadata_drift"])
        self.assertEqual(result["expected_provider_starting_branch"], "other-base")
        self.assertEqual(result["observed_provider_starting_branch"], "provider-base")

    def test_labels_never_substitute_for_exact_binding(self) -> None:
        sessions = [{"name":"sessions/wrong","normalizedState":"IN_PROGRESS","_source_repository":"owner/repo","_session_fingerprint":session_fingerprint("sessions/wrong"),"sourceStartingBranch":"wrong-branch","title":"W03 Writer"}]
        result = match_lineage_session(sessions,{"known_session_fingerprints":[],"provider_starting_branch":"work/w03"},repository="owner/repo")
        self.assertEqual(result["status"], "UNBOUND")

    def test_provider_starting_branch_is_constraint_not_lineage_identity(self) -> None:
        sessions = [{"name":"sessions/1","normalizedState":"AWAITING_USER_FEEDBACK","_source_repository":"owner/repo","_session_fingerprint":session_fingerprint("sessions/1"),"sourceStartingBranch":"provider/w03"}]
        result = match_lineage_session(sessions,{"known_session_fingerprints":[],"provider_starting_branch":"provider/w03"},repository="owner/repo")
        self.assertEqual(result["status"], "UNBOUND")
        self.assertEqual(result["reason"], "EXACT_SESSION_FINGERPRINT_REQUIRED")

    def test_two_lineages_sharing_branch_cannot_bind_same_session_without_fingerprint(self) -> None:
        fp = session_fingerprint("sessions/shared")
        sessions = [{"name":"sessions/shared","normalizedState":"IN_PROGRESS","_source_repository":"owner/repo","_session_fingerprint":fp,"sourceStartingBranch":"provider/shared"}]
        for workstream in ("W01", "W02"):
            with self.subTest(workstream=workstream):
                result = match_lineage_session(sessions,{"provider_starting_branch":"provider/shared","known_session_fingerprints":[]},repository="owner/repo")
                self.assertEqual(result["status"], "UNBOUND")
                self.assertIsNone(result["session"])

    def test_exact_known_fingerprint_can_bind_on_shared_branch(self) -> None:
        fp1 = session_fingerprint("sessions/1")
        fp2 = session_fingerprint("sessions/2")
        sessions = [
            {"name":"sessions/1","normalizedState":"IN_PROGRESS","_source_repository":"owner/repo","_session_fingerprint":fp1,"sourceStartingBranch":"provider/shared"},
            {"name":"sessions/2","normalizedState":"IN_PROGRESS","_source_repository":"owner/repo","_session_fingerprint":fp2,"sourceStartingBranch":"provider/shared"},
        ]
        result = match_lineage_session(sessions,{"provider_starting_branch":"provider/shared","known_session_fingerprints":[fp2]},repository="owner/repo")
        self.assertEqual(result["status"], "PROVEN")
        self.assertEqual(result["session_fingerprint"], fp2)

    def test_legacy_branch_only_initial_adoption_is_revoked_locally_when_exact_identity_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            fp = session_fingerprint("sessions/legacy")
            legacy_binding = {"status":"PROVEN","reason":"EXACT_GOVERNED_LINEAGE_BINDING","session_fingerprint":fp,"provider_state":"IN_PROGRESS","session":{"_source_repository":"owner/repo","sourceStartingBranch":"provider/shared"}}
            first = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="REVIEWER",binding=legacy_binding,policy={"known_session_fingerprints":[],"provider_starting_branch":"provider/shared"})
            self.assertEqual(first["generation"], 1)
            repaired = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="REVIEWER",binding={"status":"UNBOUND","reason":"EXACT_SESSION_FINGERPRINT_REQUIRED"},policy={"known_session_fingerprints":[],"provider_starting_branch":"provider/shared"})
            self.assertEqual(repaired["generation"], 0)
            self.assertEqual(repaired["binding_status"], "UNBOUND")
            self.assertIsNone(repaired["session_fingerprint"])
            self.assertTrue(repaired["legacy_branch_only_adoption_revoked"])
            lane = store.read_workstream(repaired["lane_id"])
            self.assertEqual(lane.status, "OK")
            assert lane.record is not None
            evidence = lane.record.evidence_bindings or {}
            self.assertEqual(evidence["replacement_reason"], "LEGACY_BRANCH_ONLY_ADOPTION_REVOKED")
            self.assertEqual(evidence["revoked_legacy_session_fingerprint"], fp)
            self.assertIsNone(lane.record.unknown_write_state)
            self.assertIsNone(lane.record.action_in_flight)

    def test_legacy_branch_only_adoption_is_not_revoked_with_unknown_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            fp = session_fingerprint("sessions/legacy")
            binding = {"status":"PROVEN","reason":"EXACT_GOVERNED_LINEAGE_BINDING","session_fingerprint":fp,"provider_state":"IN_PROGRESS","session":{"_source_repository":"owner/repo","sourceStartingBranch":"provider/shared"}}
            first = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="REVIEWER",binding=binding,policy={"known_session_fingerprints":[],"provider_starting_branch":"provider/shared"})
            read = store.read_workstream(first["lane_id"])
            assert read.record is not None
            record = read.record
            record.unknown_write_state = {"category":"WRITE_OUTCOME_UNKNOWN"}
            store.compare_and_swap_workstream(first["lane_id"], read.version, record)
            repaired = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="REVIEWER",binding={"status":"UNBOUND","reason":"EXACT_SESSION_FINGERPRINT_REQUIRED"},policy={"known_session_fingerprints":[],"provider_starting_branch":"provider/shared"})
            self.assertEqual(repaired["generation"], 1)
            self.assertFalse(repaired["legacy_branch_only_adoption_revoked"])

    def test_generation_changes_only_when_bound_provider_session_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            policy = {"known_session_fingerprints": []}
            fp1 = session_fingerprint("sessions/1")
            binding1 = {"status":"PROVEN","reason":"EXACT","session_fingerprint":fp1,"provider_state":"COMPLETED","session":{"_source_repository":"owner/repo","sourceStartingBranch":"provider/w01"}}
            first = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="WRITER",binding=binding1,policy=policy)
            second = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="WRITER",binding=binding1,policy=policy)
            binding2 = {**binding1,"session_fingerprint":session_fingerprint("sessions/2")}
            third = upsert_lineage_observation(store,project="P",route="P",workstream="W01",role="WRITER",binding=binding2,policy=policy)
            self.assertEqual((first["generation"],second["generation"],third["generation"]),(1,1,2))


class RecoveryCatalogTests(unittest.TestCase):
    def test_waiting_routes_same_session(self) -> None:
        result = plan_recovery({"binding_status":"PROVEN","provider_state":"AWAITING_USER_FEEDBACK","role":"WRITER","same_session_prompt_ready":True,"waiting_has_newer_or_equal_user_response":False})
        self.assertEqual(result["action"], "CONTINUE_SAME_SESSION")
        self.assertTrue(result["external_effect"])

    def test_pr_branch_drift_precedes_provider_action(self) -> None:
        result = plan_recovery({"pr_branch_match":False,"binding_status":"PROVEN","provider_state":"AWAITING_USER_FEEDBACK","role":"WRITER","same_session_prompt_ready":True})
        self.assertEqual(result["action"], "RECONCILE_GITHUB_LINEAGE_BRANCH_DRIFT")

    def test_completed_writer_with_green_ci_routes_to_reviewer(self) -> None:
        result = plan_recovery({"binding_status":"PROVEN","provider_state":"COMPLETED","role":"WRITER","candidate_sha":"a"*40,"ci_verdict":"PASS","work_remaining":True})
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")

    def test_completed_reviewer_findings_route_to_writer(self) -> None:
        result = plan_recovery({"binding_status":"PROVEN","provider_state":"COMPLETED","role":"REVIEWER","current_sha":"b"*40,"handoff":{"status":"COMPLETE","verdict":"FINDINGS","reviewed_sha":"b"*40},"work_remaining":True})
        self.assertEqual(result["action"], "ROUTE_STRUCTURED_FINDINGS_TO_WRITER_LINEAGE")

    def test_failed_session_replacement_preserves_lineage_and_requires_budget(self) -> None:
        result = plan_recovery({"binding_status":"PROVEN","provider_state":"FAILED","role":"WRITER","work_remaining":True,"new_session_budget_safe":False,"replacement_prompt_ready":True})
        self.assertEqual(result["action"], "PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertIn("TASK_BUDGET", result["stop_gate"])

    def test_stale_review_is_invalidated_first(self) -> None:
        result = plan_recovery({"binding_status":"PROVEN","provider_state":"COMPLETED","role":"REVIEWER","current_sha":"c"*40,"handoff":{"reviewed_sha":"d"*40,"verdict":"PASS"}})
        self.assertEqual(result["action"], "INVALIDATE_STALE_REVIEW_AND_ROUTE_CURRENT_SHA")


class StructuredHandoffTests(unittest.TestCase):
    def _activities(self):
        payload = {"role":"REVIEWER","workstream":"W03","status":"COMPLETE","verdict":"FINDINGS","candidate_sha":None,"reviewed_sha":"e"*40,"context_state":"OK","findings":[{"id":"F-1","severity":"HIGH","path":"src/x.py","detail":"private actionable detail"}]}
        message = f"review result\n{START_MARKER}\n{json.dumps(payload)}\n{END_MARKER}"
        return [{"name":"sessions/1/activities/2","agentMessaged":{"agentMessage":message}}]

    def test_persistable_handoff_is_sanitized(self) -> None:
        result = find_latest_structured_handoff(self._activities(),expected_workstream="W03",expected_role="REVIEWER")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["verdict"], "FINDINGS")
        self.assertNotIn("private actionable detail", str(result))

    def test_runtime_handoff_keeps_detail_only_in_memory(self) -> None:
        result = find_latest_structured_handoff_runtime(self._activities(),expected_workstream="W03",expected_role="REVIEWER")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("private actionable detail", str(result["runtime_payload"]))
        self.assertNotIn("private actionable detail", str(result["sanitized"]))


if __name__ == "__main__":
    unittest.main()
