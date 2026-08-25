from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ues.state_store import (
    DeterministicFileStateStore,
    Lease,
    OperationRecord,
    WorkstreamRuntimeRecord,
)
from ues.terminal_recovery_runtime import _exact_pending_operation_binding


class TerminalIdentityOperationReconciliationTests(unittest.TestCase):
    def test_pending_identity_confirms_exact_inflight_operation_before_lineage_binding(self):
        with tempfile.TemporaryDirectory() as root:
            store = DeterministicFileStateStore(Path(root) / "state.json")
            store.initialize()
            lane_id = "ues-lane:v1|RP04|RP04|LINEAGE%3A%3AS11%3A%3AREVIEWER"
            operation_key = "ues-v2:create-initial-lineage-session:test"
            transition_key = "a" * 64
            pending = {
                "transition_key": transition_key,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "current_generation": 0,
                "next_generation": 1,
                "source_repository": "owner/repo",
                "source_name": "sources/repo",
                "starting_branch": "main",
                "candidate_sha": "b" * 40,
                "task_spec_digest": "c" * 64,
                "provider_title_marker": transition_key[:12],
                "safe_to_blind_retry": False,
            }
            lease = Lease(
                lease_id="lease-1",
                owner="ues-initial-lineage-lifecycle",
                operation_key=operation_key,
                acquired_at="2026-08-25T00:00:00Z",
                expires_at="2026-08-25T00:03:00Z",
            )
            lane = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project="RP04",
                route="RP04",
                workstream_id="LINEAGE::S11::REVIEWER",
                evidence_bindings={
                    "role": "REVIEWER",
                    "workstream": "S11",
                    "generation": 0,
                    "session_fingerprint": None,
                    "pending_initial_lineage_transition": pending,
                },
                operation_key=operation_key,
                action_in_flight={
                    "operation_key": operation_key,
                    "owner": "ues-initial-lineage-lifecycle",
                    "started_at": "2026-08-25T00:00:00Z",
                },
                lease=lease,
            )
            saved_lane = store.compare_and_swap_workstream(lane_id, 0, lane)
            self.assertEqual(saved_lane.status, "OK")

            effect = {
                "lane_id": lane_id,
                "project": "RP04",
                "route": "RP04",
                "workstream_id": "LINEAGE::S11::REVIEWER",
                "action": "create-initial-lineage-session",
                "target": {
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "generation": "1",
                    "provider": "jules",
                    "role": "REVIEWER",
                    "starting_branch": "main",
                    "transition_key": transition_key,
                },
            }
            operation = OperationRecord(
                operation_key=operation_key,
                lane_id=lane_id,
                workstream_id="LINEAGE::S11::REVIEWER",
                action="create-initial-lineage-session",
                request_digest="d" * 64,
                state="IN_FLIGHT",
                owner="ues-initial-lineage-lifecycle",
                started_at="2026-08-25T00:00:00Z",
                updated_at="2026-08-25T00:00:00Z",
                effect_identity=effect,
            )
            store.compare_and_swap_operation(operation_key, 0, operation)

            snapshot = store.read_workstream(lane_id).record
            assert snapshot is not None
            candidate = {"lane_id": lane_id, "pending": pending, "record": snapshot}
            bind_observed_confirmed = []

            def bind(live_store, *, candidate, session_fp):
                op = live_store.read_operation(operation_key)
                bind_observed_confirmed.append(op.record.state if op.record else None)
                return {
                    "state": "IDENTITY_EXACTLY_BOUND",
                    "cas_performed": True,
                    "authoritative_readback": True,
                }

            with patch(
                "ues.terminal_recovery_runtime.record_confirmed_generation",
                return_value={"status": "ACCOUNTED"},
            ) as accounting:
                result = _exact_pending_operation_binding(
                    store,
                    candidate=candidate,
                    session_fp="f" * 64,
                    bind=bind,
                )

            self.assertEqual(result["state"], "IDENTITY_EXACTLY_BOUND")
            self.assertEqual(bind_observed_confirmed, ["CONFIRMED"])
            final_op = store.read_operation(operation_key)
            self.assertEqual(final_op.record.state, "CONFIRMED")
            final_lane = store.read_workstream(lane_id)
            self.assertIsNone(final_lane.record.action_in_flight)
            self.assertIsNone(final_lane.record.lease)
            accounting.assert_called_once_with(
                store,
                project="RP04",
                route="RP04",
                operation_key=operation_key,
                generation_transition_key=transition_key,
            )

    def test_operation_effect_mismatch_fails_closed_without_binding(self):
        class Store:
            def read_operation(self, operation_key):
                class Read:
                    status = "OK"
                    record = OperationRecord(
                        operation_key=operation_key,
                        lane_id="wrong-lane",
                        workstream_id="LINEAGE::S11::REVIEWER",
                        action="create-initial-lineage-session",
                        request_digest="d" * 64,
                        state="IN_FLIGHT",
                        owner="owner",
                        started_at="2026-08-25T00:00:00Z",
                        updated_at="2026-08-25T00:00:00Z",
                        effect_identity={"target": {}},
                    )
                return Read()

        record = WorkstreamRuntimeRecord(
            lane_id="lane",
            project="RP04",
            route="RP04",
            workstream_id="LINEAGE::S11::REVIEWER",
            evidence_bindings={"role": "REVIEWER"},
            operation_key="op",
        )
        called = []
        result = _exact_pending_operation_binding(
            Store(),
            candidate={
                "lane_id": "lane",
                "pending": {
                    "transition_key": "a" * 64,
                    "starting_branch": "main",
                    "source_repository": "owner/repo",
                    "next_generation": 1,
                },
                "record": record,
            },
            session_fp="f" * 64,
            bind=lambda *args, **kwargs: called.append(True),
        )
        self.assertEqual(result["state"], "IDENTITY_OPERATION_EFFECT_MISMATCH")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
