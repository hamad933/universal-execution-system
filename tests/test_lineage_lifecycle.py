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

    def test_exact_fingerprint_and_branch_must_both_match(self) -> None:
        expected = session_fingerprint("sessions/exact")
        sessions = [{
            "name": "sessions/exact",
            "normalizedState": "COMPLETED",
            "_source_repository": "owner/repo",
            "_session_fingerprint": expected,
            "sourceStartingBranch": "other-branch",
        }]
        rejected = match_lineage_session(
            sessions,
            {"known_session_fingerprints": [expected], "starting_branch": "expected-branch"},
            repository="owner/repo",
        )
        self.assertEqual(rejected["status"], "UNBOUND")
        accepted = match_lineage_session(
            sessions,
            {"known_session_fingerprints": [expected], "starting_branch": "other-branch"},
            repository="owner/repo",
        )
        self.assertEqual(accepted["status"], "PROVEN")
        self.assertEqual(accepted["provider_state"], "COMPLETED")

    def test_labels_never_substitute_for_exact_binding(self) -> None:
        sessions = [{
            "name": "sessions/wrong",
            "normalizedState": "IN_PROGRESS",
            "_source_repository": "owner/repo",
            "_session_fingerprint": session_fingerprint("sessions/wrong"),
            "sourceStartingBranch": "wrong-branch",
            "title": "W03 Writer",
        }]
        result = match_lineage_session(
            sessions,
            {"known_session_fingerprints": [], "starting_branch": "work/w03"},
            repository="owner/repo",
        )
        self.assertEqual(result["status"], "UNBOUND")

    def test_exact_starting_branch_binds_when_no_fingerprint_exists(self) -> None:
        sessions = [{
            "name": "sessions/1",
            "normalizedState": "AWAITING_USER_FEEDBACK",
            "_source_repository": "owner/repo",
            "_session_fingerprint": session_fingerprint("sessions/1"),
            "sourceStartingBranch": "work/w03",
        }]
        result = match_lineage_session(sessions, {"known_session_fingerprints": [], "starting_branch": "work/w03"}, repository="owner/repo")
        self.assertEqual(result["status"], "PROVEN")
        self.assertEqual(result["continuation_disposition"], "REUSE_SAME_SESSION")

    def test_multiple_exact_active_matches_are_ambiguous(self) -> None:
        sessions = [{
            "name": f"sessions/{index}",
            "normalizedState": "IN_PROGRESS",
            "_source_repository": "owner/repo",
            "_session_fingerprint": session_fingerprint(f"sessions/{index}"),
            "sourceStartingBranch": "work/shared",
            "updateTime": "2026-08-24T00:00:00Z",
        } for index in (1, 2)]
        result = match_lineage_session(sessions, {"starting_branch": "work/shared", "known_session_fingerprints": []}, repository="owner/repo")
        self.assertEqual(result["status"], "AMBIGUOUS")

    def test_generation_changes_only_when_bound_provider_session_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            policy = {"known_session_fingerprints": []}
            fp1 = session_fingerprint("sessions/1")
            binding1 = {
                "status": "PROVEN", "reason": "EXACT", "session_fingerprint": fp1, "provider_state": "COMPLETED",
                "session": {"_source_repository": "owner/repo", "sourceStartingBranch": "work/w01"},
            }
            first = upsert_lineage_observation(store, project="P", route="P", workstream="W01", role="WRITER", binding=binding1, policy=policy)
            second = upsert_lineage_observation(store, project="P", route="P", workstream="W01", role="WRITER", binding=binding1, policy=policy)
            binding2 = {**binding1, "session_fingerprint": session_fingerprint("sessions/2")}
            third = upsert_lineage_observation(store, project="P", route="P", workstream="W01", role="WRITER", binding=binding2, policy=policy)
            self.assertEqual((first["generation"], second["generation"], third["generation"]), (1, 1, 2))


class RecoveryCatalogTests(unittest.TestCase):
    def test_waiting_routes_same_session(self) -> None:
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "AWAITING_USER_FEEDBACK", "role": "WRITER",
            "same_session_prompt_ready": True, "waiting_has_newer_or_equal_user_response": False,
        })
        self.assertEqual(result["action"], "CONTINUE_SAME_SESSION")
        self.assertTrue(result["external_effect"])

    def test_completed_writer_with_green_ci_routes_to_reviewer(self) -> None:
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a" * 40, "ci_verdict": "PASS", "work_remaining": True,
        })
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")

    def test_completed_reviewer_findings_route_to_writer(self) -> None:
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "REVIEWER",
            "current_sha": "b" * 40,
            "handoff": {"status": "COMPLETE", "verdict": "FINDINGS", "reviewed_sha": "b" * 40},
            "work_remaining": True,
        })
        self.assertEqual(result["action"], "ROUTE_STRUCTURED_FINDINGS_TO_WRITER_LINEAGE")

    def test_failed_session_replacement_preserves_lineage_and_requires_budget(self) -> None:
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER", "work_remaining": True,
            "new_session_budget_safe": False, "replacement_prompt_ready": True,
        })
        self.assertEqual(result["action"], "PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertIn("TASK_BUDGET", result["stop_gate"])

    def test_stale_review_is_invalidated_first(self) -> None:
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "REVIEWER",
            "current_sha": "c" * 40, "handoff": {"reviewed_sha": "d" * 40, "verdict": "PASS"},
        })
        self.assertEqual(result["action"], "INVALIDATE_STALE_REVIEW_AND_ROUTE_CURRENT_SHA")


class StructuredHandoffTests(unittest.TestCase):
    def _activities(self):
        payload = {
            "role": "REVIEWER", "workstream": "W03", "status": "COMPLETE", "verdict": "FINDINGS",
            "candidate_sha": None, "reviewed_sha": "e" * 40, "context_state": "OK",
            "findings": [{"id": "F-1", "severity": "HIGH", "path": "src/x.py", "detail": "private actionable detail"}],
        }
        message = f"review result\n{START_MARKER}\n{json.dumps(payload)}\n{END_MARKER}"
        return [{"name": "sessions/1/activities/2", "agentMessaged": {"agentMessage": message}}]

    def test_persistable_handoff_is_sanitized(self) -> None:
        result = find_latest_structured_handoff(self._activities(), expected_workstream="W03", expected_role="REVIEWER")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["verdict"], "FINDINGS")
        self.assertNotIn("private actionable detail", str(result))

    def test_runtime_handoff_keeps_detail_only_in_memory(self) -> None:
        result = find_latest_structured_handoff_runtime(self._activities(), expected_workstream="W03", expected_role="REVIEWER")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("private actionable detail", str(result["runtime_payload"]))
        self.assertNotIn("private actionable detail", str(result["sanitized"]))


if __name__ == "__main__":
    unittest.main()
