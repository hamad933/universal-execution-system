from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.initial_lineage_reconciliation import reconcile_unknown_initial_lineage
from ues.lineage_registry import lineage_lane_id
from ues.state_store import DeterministicFileStateStore, OperationRecord, WorkstreamRuntimeRecord
from ues.task_budget_accounting import read_budget_accounting


class InitialLineageReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.lane_id = lineage_lane_id("RP01", "RP01", "W11", "WRITER")
        self.operation_key = "initial-op-1"
        self.marker = "abcdef123456"
        record = WorkstreamRuntimeRecord(
            lane_id=self.lane_id,
            project="RP01",
            route="RP01",
            workstream_id="LINEAGE::W11::WRITER",
            activation_mode="SHADOW",
            evidence_bindings={
                "generation": 0,
                "session_fingerprint": None,
                "pending_initial_lineage_transition": {
                    "transition_key": self.marker + "0" * 52,
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "source_repository": "hamad933/Bayt-Style",
                    "source_name": "sources/github/hamad933/Bayt-Style",
                    "starting_branch": "main",
                    "candidate_sha": "a" * 40,
                    "task_spec_digest": "b" * 64,
                    "provider_title_marker": self.marker,
                    "safe_to_blind_retry": False,
                },
            },
            unknown_write_state={
                "operation_key": self.operation_key,
                "category": "INITIAL_LINEAGE_CREATE_OUTCOME_UNKNOWN",
                "safe_to_blind_retry": False,
            },
        )
        self.store.compare_and_swap_workstream(self.lane_id, 0, record)
        operation = OperationRecord(
            operation_key=self.operation_key,
            lane_id=self.lane_id,
            workstream_id="LINEAGE::W11::WRITER",
            action="create-initial-lineage-session",
            request_digest="c" * 64,
            state="UNKNOWN",
            owner="ues-initial-lineage-lifecycle",
            started_at="2026-08-24T18:00:00Z",
            updated_at="2026-08-24T18:00:01Z",
            reconciliation_required=True,
        )
        self.store.compare_and_swap_operation(self.operation_key, 0, operation)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def session(self, name: str = "sessions/recovered") -> dict:
        return {
            "name": name,
            "title": f"RP01 W11 WRITER G1 [{self.marker}]",
            "_source_repository": "hamad933/Bayt-Style",
            "sourceStartingBranch": "main",
            "normalizedState": "COMPLETED",
        }

    def reconcile(self, inventory):
        return reconcile_unknown_initial_lineage(
            self.store,
            project="RP01",
            route="RP01",
            workstream="W11",
            role="WRITER",
            inventory=inventory,
            authority_event_id="RP01-AUTH-001",
            policy_provenance={"source": "DRIVE_CURRENT_STATE"},
        )

    def test_exact_single_provider_match_is_adopted_without_second_create(self):
        result = self.reconcile([self.session()])
        self.assertEqual(result["decision"], "AMBIGUOUS_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["match_count"], 1)

        lane = self.store.read_workstream(self.lane_id)
        self.assertEqual(lane.status, "OK")
        assert lane.record is not None
        evidence = lane.record.evidence_bindings or {}
        self.assertEqual(evidence["generation"], 1)
        self.assertEqual(evidence["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
        self.assertNotIn("pending_initial_lineage_transition", evidence)
        self.assertIsNone(lane.record.unknown_write_state)
        self.assertEqual(lane.record.activation_mode, "SHADOW")

        operation = self.store.read_operation(self.operation_key)
        self.assertEqual(operation.status, "OK")
        assert operation.record is not None
        self.assertEqual(operation.record.state, "CONFIRMED")
        self.assertIsNotNone(operation.record.authoritative_readback)

        budget = read_budget_accounting(self.store, project="RP01", route="RP01")
        self.assertEqual(budget["ues_confirmed_generation_count"], 1)

    def test_zero_match_remains_unknown_and_never_retries(self):
        result = self.reconcile([])
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_UNKNOWN_NOT_YET_OBSERVED")
        self.assertEqual(result["match_count"], 0)
        self.assertFalse(result["provider_write_attempted"])
        self.assertFalse(result["safe_to_blind_retry"])
        lane = self.store.read_workstream(self.lane_id)
        assert lane.record is not None
        self.assertIsNotNone(lane.record.unknown_write_state)
        self.assertEqual((lane.record.evidence_bindings or {})["generation"], 0)

    def test_multiple_exact_matches_remain_ambiguous(self):
        result = self.reconcile([self.session("sessions/a"), self.session("sessions/b")])
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_RECONCILIATION_AMBIGUOUS_DUPLICATE")
        self.assertEqual(result["match_count"], 2)
        self.assertFalse(result["provider_write_attempted"])
        self.assertFalse(result["safe_to_blind_retry"])
        lane = self.store.read_workstream(self.lane_id)
        assert lane.record is not None
        self.assertIsNotNone(lane.record.unknown_write_state)

    def test_wrong_repo_ref_or_marker_is_not_adopted(self):
        wrong = [
            {**self.session("sessions/repo"), "_source_repository": "other/repo"},
            {**self.session("sessions/ref"), "sourceStartingBranch": "other"},
            {**self.session("sessions/marker"), "title": "RP01 W11 WRITER G1 [different]"},
        ]
        result = self.reconcile(wrong)
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_UNKNOWN_NOT_YET_OBSERVED")
        self.assertEqual(result["match_count"], 0)


if __name__ == "__main__":
    unittest.main()
