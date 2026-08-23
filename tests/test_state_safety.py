import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.idempotency import (
    canonical_request_digest,
    correction_packet_operation_key,
    reviewer_dispatch_operation_key,
    task_session_operation_key,
    waiting_answer_operation_key,
)
from ues.operation_records import (
    render_receipt_comment,
    sanitize_receipt,
    trusted_operation_records,
)
from ues.state_store import (
    DeterministicFileStateStore,
    LeaseCollision,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 23, 19, 0, tzinfo=UTC)


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.json"
        self.store = DeterministicFileStateStore(self.path, clock=lambda: T0)
        self.store.initialize()
        record = WorkstreamRuntimeRecord(
            workstream_id="WS-A",
            project="UES",
            route="PERSONAL:UES",
            activation_mode="CANARY",
        )
        self.store.compare_and_swap_workstream("WS-A", 0, record)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lease_collision(self):
        first = self.store.acquire_lease(
            "WS-A", "runner-1", "op-1", 60, now=T0
        )
        self.assertTrue(first.lease.lease_id)
        with self.assertRaises(LeaseCollision):
            self.store.acquire_lease(
                "WS-A", "runner-2", "op-2", 60, now=T0
            )

    def test_stale_lease_recovery(self):
        self.store.acquire_lease(
            "WS-A", "runner-1", "op-1", 10, now=T0
        )
        recovered = self.store.acquire_lease(
            "WS-A",
            "runner-2",
            "op-2",
            60,
            now=T0 + timedelta(seconds=11),
        )
        self.assertTrue(recovered.stale_recovered)
        self.assertEqual(recovered.lease.owner, "runner-2")

    def test_duplicate_operation_prevention(self):
        digest = canonical_request_digest({"message": "bounded answer"})
        key = waiting_answer_operation_key(
            project="UES",
            workstream_id="WS-A",
            session_id="s1",
            waiting_activity_id="a9",
            answer_digest=digest,
        )
        first = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner-1",
            operation_key=key,
            action="waiting-answer",
            request_digest=digest,
            ttl_seconds=60,
            now=T0,
        )
        self.assertEqual(first["decision"], "CLAIMED")
        second = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner-2",
            operation_key=key,
            action="waiting-answer",
            request_digest=digest,
            ttl_seconds=60,
            now=T0,
        )
        self.assertEqual(second["decision"], "RECONCILE_REQUIRED")
        self.assertFalse(second["mutation_allowed"])

    def test_unknown_write_outcome_requires_readback(self):
        digest = canonical_request_digest({"send": "packet"})
        key = correction_packet_operation_key(
            project="UES",
            workstream_id="WS-A",
            writer_session_id="writer-1",
            reviewer_session_id="reviewer-1",
            candidate_sha="a" * 40,
            findings_digest=digest,
        )
        claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner",
            operation_key=key,
            action="reviewer-writer-correction",
            request_digest=digest,
            ttl_seconds=60,
            now=T0,
        )
        record_unknown_write(
            self.store,
            workstream_id="WS-A",
            operation_key=key,
            result={"transport": "timeout"},
            now=T0,
        )
        retry = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner-2",
            operation_key=key,
            action="reviewer-writer-correction",
            request_digest=digest,
            ttl_seconds=60,
            now=T0 + timedelta(seconds=61),
        )
        self.assertEqual(retry["decision"], "RECONCILE_REQUIRED")

    def test_readback_before_retry(self):
        digest = canonical_request_digest({"dispatch": "reviewer"})
        key = reviewer_dispatch_operation_key(
            project="UES",
            workstream_id="WS-A",
            candidate_sha="b" * 40,
            reviewer_lineage="reviewer",
            dispatch_target="session-r",
        )
        claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner",
            operation_key=key,
            action="reviewer-dispatch",
            request_digest=digest,
            ttl_seconds=30,
            now=T0,
        )
        record_unknown_write(
            self.store,
            workstream_id="WS-A",
            operation_key=key,
            result={"error": "connection-reset"},
            now=T0,
        )
        readback = record_authoritative_readback(
            self.store,
            workstream_id="WS-A",
            operation_key=key,
            observed=False,
            evidence={
                "session_id": "session-r",
                "activity_present": False,
            },
            now=T0 + timedelta(seconds=5),
        )
        self.assertEqual(
            readback.record.state, "RECONCILED_NOT_OBSERVED"
        )
        retry = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner-2",
            operation_key=key,
            action="reviewer-dispatch",
            request_digest=digest,
            ttl_seconds=30,
            now=T0 + timedelta(seconds=6),
        )
        self.assertEqual(retry["decision"], "CLAIMED")
        self.assertEqual(self.store.read_operation(key).record.attempt, 2)

    def test_state_version_conflict(self):
        read = self.store.read_workstream("WS-A")
        self.store.compare_and_swap_workstream(
            "WS-A", read.version, read.record
        )
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(
                "WS-A", read.version, read.record
            )

    def test_restart_reload_preserves_inflight_state(self):
        key = task_session_operation_key(
            project="UES",
            workstream_id="WS-A",
            intent="recommendation",
            task_kind="writer",
            lineage="L1",
            authority_event_id="evt-7",
        )
        digest = canonical_request_digest({"task": "writer"})
        claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner",
            operation_key=key,
            action="task-session-recommendation",
            request_digest=digest,
            ttl_seconds=120,
            now=T0,
        )
        reloaded = DeterministicFileStateStore(
            self.path, clock=lambda: T0
        )
        ws = reloaded.read_workstream("WS-A")
        op = reloaded.read_operation(key)
        self.assertEqual(ws.record.operation_key, key)
        self.assertEqual(ws.record.lease.operation_key, key)
        self.assertEqual(op.record.state, "IN_FLIGHT")

    def test_missing_and_corrupt_state_fail_closed(self):
        missing = DeterministicFileStateStore(
            Path(self.tmp.name) / "missing.json"
        )
        read = missing.read_workstream("WS-X")
        self.assertEqual(read.effective_activation_mode, "SHADOW")
        self.assertFalse(read.mutation_allowed)

        corrupt_path = Path(self.tmp.name) / "corrupt.json"
        corrupt_path.write_text("{not json", encoding="utf-8")
        corrupt = DeterministicFileStateStore(corrupt_path)
        read = corrupt.read_workstream("WS-X")
        self.assertEqual(read.status, "CORRUPT")
        self.assertEqual(read.effective_activation_mode, "SHADOW")
        self.assertFalse(read.mutation_allowed)

    def test_receipt_sanitization(self):
        receipt = {
            "operation_id": "op-1",
            "state": "UNKNOWN",
            "start_sha": "a" * 40,
            "session_id": "session-safe",
            "action": "send-message",
            "Authorization": "Bearer super-secret-token",
            "nested": {
                "api_key": "abcdef",
                "message": "Bearer leaked.value",
            },
        }
        safe = sanitize_receipt(receipt)
        serialized = json.dumps(safe)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("abcdef", serialized)
        self.assertNotIn("leaked.value", serialized)

        body = render_receipt_comment(receipt)
        parsed = trusted_operation_records(
            [{"author": "github-actions[bot]", "body": body}]
        )[0]
        self.assertEqual(parsed["session_id"], "session-safe")
        self.assertEqual(parsed["start_sha"], "a" * 40)
        self.assertEqual(parsed["Authorization"], "[REDACTED]")

    def test_blocked_lane_does_not_lock_unrelated_workstream(self):
        other = WorkstreamRuntimeRecord(
            workstream_id="WS-B",
            project="UES",
            activation_mode="CANARY",
        )
        self.store.compare_and_swap_workstream("WS-B", 0, other)
        self.store.acquire_lease(
            "WS-A", "runner-a", "op-a", 60, now=T0
        )
        acquired = self.store.acquire_lease(
            "WS-B", "runner-b", "op-b", 60, now=T0
        )
        self.assertEqual(acquired.lease.operation_key, "op-b")

    def test_operation_keys_separate_semantically_distinct_actions(self):
        common = dict(project="UES", workstream_id="WS-A")
        waiting = waiting_answer_operation_key(
            **common,
            session_id="s",
            waiting_activity_id="a",
            answer_digest="d",
        )
        correction = correction_packet_operation_key(
            **common,
            writer_session_id="w",
            reviewer_session_id="r",
            candidate_sha="c",
            findings_digest="d",
        )
        review = reviewer_dispatch_operation_key(
            **common,
            candidate_sha="c",
            reviewer_lineage="r",
            dispatch_target="target",
        )
        task = task_session_operation_key(
            **common,
            intent="create",
            task_kind="reviewer",
            lineage="r",
            authority_event_id="evt",
        )
        self.assertEqual(len({waiting, correction, review, task}), 4)


if __name__ == "__main__":
    unittest.main()
