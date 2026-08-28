from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from ues.lineage_registry import lineage_lane_id
from ues.stale_initial_lineage_reconciliation import reconcile_stale_initial_lineage_lane
from ues.state_store import DeterministicFileStateStore, OperationRecord, WorkstreamRuntimeRecord


class StaleInitialLineageReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.project = "RP04"
        self.route = "RP04"
        self.workstream = "RP04-IPA-S11-001"
        self.role = "REVIEWER"
        self.repository = "hamad933/Real-Estate-Assets-Control-"
        self.source_name = "sources/github/hamad933/Real-Estate-Assets-Control-"
        self.branch = "feature/rp04-imp-w08-human-grade-portfolio-polish"
        self.transition_key = "123456789abc" + "0" * 52
        self.operation_key = "stale-initial-op"
        self.task_digest = "b" * 64
        self.lane_id = lineage_lane_id(self.project, self.route, self.workstream, self.role)
        source_fp = sha256(self.source_name.encode("utf-8")).hexdigest()
        receipt = {
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "generation": 1,
            "source_fingerprint": source_fp,
            "starting_branch": self.branch,
            "task_spec_digest": self.task_digest,
            "transition_key": self.transition_key,
            "operation_key": self.operation_key,
            "effect_identity": {
                "target": {
                    "provider": "jules",
                    "role": self.role,
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "generation": "1",
                    "source_fingerprint": source_fp,
                    "starting_branch": self.branch,
                    "transition_key": self.transition_key,
                }
            },
        }
        lane = WorkstreamRuntimeRecord(
            lane_id=self.lane_id,
            project=self.project,
            route=self.route,
            workstream_id=f"LINEAGE::{self.workstream}::{self.role}",
            activation_mode="SHADOW",
            evidence_bindings={
                "generation": 0,
                "session_fingerprint": None,
                "current_candidate_sha": "5" * 40,
            },
            operation_key=self.operation_key,
            operation_receipt=receipt,
        )
        self.store.compare_and_swap_workstream(self.lane_id, 0, lane)
        op = OperationRecord(
            operation_key=self.operation_key,
            lane_id=self.lane_id,
            workstream_id=f"LINEAGE::{self.workstream}::{self.role}",
            action="create-initial-lineage-session",
            request_digest="c" * 64,
            state="IN_FLIGHT",
            owner="ues-initial-lineage-lifecycle",
            started_at="2026-08-25T20:00:00Z",
            updated_at="2026-08-25T20:00:01Z",
            receipt=receipt,
            effect_identity=receipt["effect_identity"],
        )
        self.store.compare_and_swap_operation(self.operation_key, 0, op)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def session(self, name: str = "sessions/recovered") -> dict:
        return {
            "name": name,
            "title": f"RP04 S11 REVIEWER G1 [{self.transition_key[:12]}]",
            "_source_repository": self.repository,
            "sourceStartingBranch": self.branch,
            "normalizedState": "COMPLETED",
        }

    def reconcile(self, inventory):
        return reconcile_stale_initial_lineage_lane(
            self.store,
            project=self.project,
            route=self.route,
            workstream=self.workstream,
            role=self.role,
            repository=self.repository,
            authority_event_id="RP04-AUTH-TEST",
            inventory=inventory,
            source_name=self.source_name,
            authority_starting_branch=self.branch,
        )

    def test_single_exact_match_reconciles_without_provider_write(self):
        result = self.reconcile([self.session()])
        self.assertEqual(result["decision"], "STALE_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

        lane = self.store.read_workstream(self.lane_id)
        self.assertEqual(lane.status, "OK")
        assert lane.record is not None
        evidence = lane.record.evidence_bindings or {}
        self.assertEqual(evidence["generation"], 1)
        self.assertTrue(evidence["session_fingerprint"])
        self.assertEqual(evidence["binding_status"], "PROVEN")

        operation = self.store.read_operation(self.operation_key)
        self.assertEqual(operation.status, "OK")
        assert operation.record is not None
        self.assertEqual(operation.record.state, "CONFIRMED")
        self.assertIsNotNone(operation.record.authoritative_readback)

    def test_zero_match_remains_unresolved_and_does_not_mutate_provider(self):
        result = self.reconcile([])
        self.assertEqual(result["decision"], "STALE_INITIAL_LINEAGE_NOT_YET_OBSERVED")
        self.assertFalse(result["provider_write_attempted"])
        operation = self.store.read_operation(self.operation_key)
        assert operation.record is not None
        self.assertEqual(operation.record.state, "IN_FLIGHT")

    def test_multiple_matches_remain_ambiguous(self):
        result = self.reconcile([self.session("sessions/a"), self.session("sessions/b")])
        self.assertEqual(result["decision"], "STALE_INITIAL_LINEAGE_AMBIGUOUS_DUPLICATE")
        self.assertEqual(result["match_count"], 2)
        self.assertFalse(result["provider_write_attempted"])

    def test_wrong_branch_or_repository_never_binds(self):
        wrong = [
            {**self.session("sessions/repo"), "_source_repository": "other/repo"},
            {**self.session("sessions/branch"), "sourceStartingBranch": "other"},
        ]
        result = self.reconcile(wrong)
        self.assertEqual(result["decision"], "STALE_INITIAL_LINEAGE_NOT_YET_OBSERVED")
        self.assertFalse(result["provider_write_attempted"])


if __name__ == "__main__":
    unittest.main()
