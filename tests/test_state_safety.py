import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.idempotency import (
    canonical_request_digest,
    correction_packet_effect_identity,
    effect_operation_key,
    reviewer_dispatch_effect_identity,
    task_session_effect_identity,
    waiting_answer_effect_identity,
    waiting_answer_operation_key,
)
from ues.operation_records import (
    render_receipt_comment,
    sanitize_receipt,
    trusted_operation_records,
)
from ues.state_store import (
    CanaryGrant,
    DeterministicFileStateStore,
    LeaseCollision,
    MutationAuthorization,
    StateStoreCapabilities,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    claim_operation,
    production_state_store_assessment,
    record_authoritative_readback,
    record_unknown_write,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 23, 19, 0, tzinfo=UTC)
ROUTE = "PERSONAL:UES"


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.json"
        self.store = DeterministicFileStateStore(self.path, clock=lambda: T0)
        self.store.initialize()
        self._put_workstream("WS-A", mode="CANARY")

    def tearDown(self):
        self.tmp.cleanup()

    def _put_workstream(self, workstream_id, *, mode="CANARY", grants=None):
        read = self.store.read_workstream(workstream_id)
        expected = read.version if read.status == "OK" else 0
        record = WorkstreamRuntimeRecord(
            workstream_id=workstream_id,
            project="UES",
            route=ROUTE,
            activation_mode=mode,
            canary_grants=list(grants or []),
        )
        return self.store.compare_and_swap_workstream(workstream_id, expected, record)

    def _grant(self, effect, *, event="grant-1", expires=None, consumed=0, expected_start=None):
        return CanaryGrant(
            authority_event_id=event,
            project=effect.project,
            route=ROUTE,
            workstream_id=effect.workstream_id,
            effect_type=effect.action,
            target=dict(effect.target),
            issued_at=(T0 - timedelta(minutes=1)).isoformat(),
            expires_at=(expires or (T0 + timedelta(minutes=10))).isoformat(),
            maximum_effect_count=1,
            expected_start=expected_start,
            consumed_count=consumed,
            consumed_at=(T0 - timedelta(seconds=1)).isoformat() if consumed else None,
            consumed_operation_keys=["prior-op"] if consumed else [],
        )

    def _set_grant(self, effect, **kwargs):
        grant = self._grant(effect, **kwargs)
        self._put_workstream(effect.workstream_id, mode="CANARY", grants=[grant])
        return grant

    def _claim(self, effect, payload, *, owner="runner-1", observed_start=None, now=T0):
        digest = canonical_request_digest(payload)
        return claim_operation(
            self.store,
            workstream_id=effect.workstream_id,
            owner=owner,
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=digest,
            ttl_seconds=60,
            effect_identity=effect,
            observed_start=observed_start,
            now=now,
        )

    def test_same_waiting_activity_changed_answer_is_collision(self):
        effect = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a9"
        )
        self._set_grant(effect)
        first_digest = canonical_request_digest({"answer": "first"})
        second_digest = canonical_request_digest({"answer": "changed"})
        key1 = waiting_answer_operation_key(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a9", answer_digest=first_digest
        )
        key2 = waiting_answer_operation_key(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a9", answer_digest=second_digest
        )
        self.assertEqual(key1, key2)
        first = self._claim(effect, {"answer": "first"})
        self.assertEqual(first["decision"], "CLAIMED")
        second = self._claim(effect, {"answer": "changed"}, owner="runner-2")
        self.assertEqual(second["decision"], "OPERATION_ID_COLLISION")
        self.assertFalse(second["mutation_allowed"])

    def test_exact_same_waiting_answer_replay_is_suppressed(self):
        effect = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a9"
        )
        self._set_grant(effect)
        payload = {"answer": "bounded"}
        first = self._claim(effect, payload)
        self.assertEqual(first["decision"], "CLAIMED")
        record_authoritative_readback(
            self.store,
            workstream_id="WS-A",
            operation_key=effect_operation_key(effect),
            observed=True,
            evidence={"session_id": "s1", "activity_id": "delivered-1"},
            now=T0 + timedelta(seconds=2),
        )
        replay = self._claim(effect, payload, owner="runner-2", now=T0 + timedelta(seconds=3))
        self.assertEqual(replay["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertFalse(replay["mutation_allowed"])

    def test_canary_action_outside_granted_workstream_denied(self):
        granted = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a1"
        )
        self._set_grant(granted)
        outside = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-B", session_id="s1", waiting_activity_id="a1"
        )
        result = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner",
            operation_key=effect_operation_key(outside),
            action=outside.action,
            request_digest=canonical_request_digest({"answer": "x"}),
            ttl_seconds=60,
            effect_identity=outside,
            now=T0,
        )
        self.assertEqual(result["decision"], "EFFECT_IDENTITY_LANE_MISMATCH")

    def test_canary_wrong_session_target_denied(self):
        granted = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a1"
        )
        self._set_grant(granted)
        wrong = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s2", waiting_activity_id="a1"
        )
        result = self._claim(wrong, {"answer": "x"})
        self.assertEqual(result["decision"], "CANARY_GRANT_NOT_FOUND")

    def test_expired_grant_denied(self):
        effect = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a1"
        )
        self._set_grant(effect, expires=T0 - timedelta(seconds=1))
        result = self._claim(effect, {"answer": "x"})
        self.assertEqual(result["decision"], "CANARY_GRANT_EXPIRED")

    def test_consumed_grant_denied(self):
        effect = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a1"
        )
        self._set_grant(effect, consumed=1)
        result = self._claim(effect, {"answer": "x"})
        self.assertEqual(result["decision"], "CANARY_GRANT_CONSUMED")

    def test_matching_one_shot_grant_allowed_once(self):
        effect = reviewer_dispatch_effect_identity(
            project="UES",
            workstream_id="WS-A",
            candidate_sha="a" * 40,
            reviewer_lineage="reviewer",
            dispatch_target="session-r",
        )
        self._set_grant(effect, expected_start={"head_sha": "a" * 40})
        result = self._claim(
            effect,
            {"dispatch": "reviewer"},
            observed_start={"head_sha": "a" * 40, "provider_state": "IN_PROGRESS"},
        )
        self.assertEqual(result["decision"], "CLAIMED")
        ws = self.store.read_workstream("WS-A").record
        self.assertEqual(ws.canary_grants[0].consumed_count, 1)
        self.assertEqual(ws.canary_grants[0].consumed_operation_keys, [effect_operation_key(effect)])

    def test_canary_mode_without_grant_denied(self):
        effect = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s1", waiting_activity_id="a1"
        )
        result = self._claim(effect, {"answer": "x"})
        self.assertEqual(result["decision"], "CANARY_GRANT_NOT_FOUND")

    def test_active_auto_safe_requires_action_authority(self):
        effect = correction_packet_effect_identity(
            project="UES",
            workstream_id="WS-A",
            writer_session_id="writer-1",
            reviewer_session_id="reviewer-1",
            candidate_sha="a" * 40,
        )
        self._put_workstream("WS-A", mode="ACTIVE_AUTO_SAFE")
        no_auth = self._claim(effect, {"findings": ["x"]})
        self.assertEqual(no_auth["decision"], "ACTION_AUTHORITY_REQUIRED")
        auth = MutationAuthorization(
            effect_identity=effect,
            authority_event_id="policy-7",
            project_policy_authorized=True,
            exact_binding_proven=True,
            evidence_verified=True,
            expires_at=(T0 + timedelta(minutes=2)).isoformat(),
        )
        result = claim_operation(
            self.store,
            workstream_id="WS-A",
            owner="runner",
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=canonical_request_digest({"findings": ["x"]}),
            ttl_seconds=60,
            effect_identity=effect,
            authorization=auth,
            now=T0,
        )
        self.assertEqual(result["decision"], "CLAIMED")

    def test_restart_after_ambiguous_send_requires_readback(self):
        effect = reviewer_dispatch_effect_identity(
            project="UES",
            workstream_id="WS-A",
            candidate_sha="b" * 40,
            reviewer_lineage="reviewer",
            dispatch_target="session-r",
        )
        self._set_grant(effect)
        payload = {"dispatch": "reviewer"}
        first = self._claim(effect, payload)
        self.assertEqual(first["decision"], "CLAIMED")

        runner_b = DeterministicFileStateStore(self.path, clock=lambda: T0 + timedelta(seconds=90))
        op = runner_b.read_operation(effect_operation_key(effect))
        self.assertEqual(op.record.state, "IN_FLIGHT")
        blocked = claim_operation(
            runner_b,
            workstream_id="WS-A",
            owner="runner-b",
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=canonical_request_digest(payload),
            ttl_seconds=60,
            effect_identity=effect,
            now=T0 + timedelta(seconds=90),
        )
        self.assertEqual(blocked["decision"], "RECONCILE_REQUIRED")

        readback = record_authoritative_readback(
            runner_b,
            workstream_id="WS-A",
            operation_key=effect_operation_key(effect),
            observed=False,
            evidence={"session_id": "session-r", "activity_present": False},
            now=T0 + timedelta(seconds=91),
        )
        self.assertEqual(readback.record.state, "RECONCILED_NOT_OBSERVED")
        retry = claim_operation(
            runner_b,
            workstream_id="WS-A",
            owner="runner-b",
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=canonical_request_digest(payload),
            ttl_seconds=60,
            effect_identity=effect,
            now=T0 + timedelta(seconds=92),
        )
        self.assertEqual(retry["decision"], "CANARY_GRANT_CONSUMED")

    def test_unknown_write_outcome_requires_readback(self):
        effect = correction_packet_effect_identity(
            project="UES",
            workstream_id="WS-A",
            writer_session_id="writer-1",
            reviewer_session_id="reviewer-1",
            candidate_sha="a" * 40,
        )
        self._set_grant(effect)
        self._claim(effect, {"findings": ["x"]})
        record_unknown_write(
            self.store,
            workstream_id="WS-A",
            operation_key=effect_operation_key(effect),
            result={"transport": "timeout"},
            now=T0,
        )
        retry = self._claim(effect, {"findings": ["x"]}, owner="runner-2", now=T0 + timedelta(seconds=61))
        self.assertEqual(retry["decision"], "RECONCILE_REQUIRED")

    def test_cross_run_backend_conformance_contract(self):
        local = production_state_store_assessment(self.store)
        self.assertFalse(local["ready_for_cross_run_production"])
        self.assertIn("survives_runner_replacement", local["missing_capabilities"])
        self.assertIn("durable_operation_records", local["missing_capabilities"])

        conformant = StateStoreCapabilities(
            backend_name="future-cross-run-cas-backend",
            survives_runner_replacement=True,
            atomic_compare_and_swap=True,
            versioned_state=True,
            lane_local_leases=True,
            durable_operation_records=True,
            authoritative_restart_reconciliation=True,
            conflict_detection=True,
        )
        assessment = production_state_store_assessment(conformant)
        self.assertTrue(assessment["ready_for_cross_run_production"])
        self.assertEqual(assessment["missing_capabilities"], [])
        self.assertIn("atomicity", assessment["serialization_requirements"])
        self.assertIn("restart_rule", assessment["serialization_requirements"])

    def test_state_version_conflict(self):
        read = self.store.read_workstream("WS-A")
        self.store.compare_and_swap_workstream("WS-A", read.version, read.record)
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream("WS-A", read.version, read.record)

    def test_unrelated_lane_lease_remains_available(self):
        self._put_workstream("WS-B", mode="CANARY")
        self.store.acquire_lease("WS-A", "runner-a", "op-a", 60, now=T0)
        acquired = self.store.acquire_lease("WS-B", "runner-b", "op-b", 60, now=T0)
        self.assertEqual(acquired.lease.operation_key, "op-b")

    def test_lease_collision_and_stale_recovery(self):
        self.store.acquire_lease("WS-A", "runner-1", "op-1", 10, now=T0)
        with self.assertRaises(LeaseCollision):
            self.store.acquire_lease("WS-A", "runner-2", "op-2", 60, now=T0)
        recovered = self.store.acquire_lease(
            "WS-A", "runner-2", "op-2", 60, now=T0 + timedelta(seconds=11)
        )
        self.assertTrue(recovered.stale_recovered)

    def test_receipt_sanitization(self):
        receipt = {
            "operation_id": "op-1",
            "state": "UNKNOWN",
            "start_sha": "a" * 40,
            "session_id": "session-safe",
            "action": "send-message",
            "Authorization": "Bearer super-secret-token",
            "nested": {"api_key": "abcdef", "message": "Bearer leaked.value"},
        }
        safe = sanitize_receipt(receipt)
        serialized = json.dumps(safe)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("abcdef", serialized)
        self.assertNotIn("leaked.value", serialized)
        body = render_receipt_comment(receipt)
        parsed = trusted_operation_records([{"author": "github-actions[bot]", "body": body}])[0]
        self.assertEqual(parsed["session_id"], "session-safe")
        self.assertEqual(parsed["Authorization"], "[REDACTED]")

    def test_missing_and_corrupt_state_fail_closed(self):
        missing = DeterministicFileStateStore(Path(self.tmp.name) / "missing.json")
        read = missing.read_workstream("WS-X")
        self.assertEqual(read.effective_activation_mode, "SHADOW")
        self.assertFalse(read.mutation_allowed)

        corrupt_path = Path(self.tmp.name) / "corrupt.json"
        corrupt_path.write_text("{not json", encoding="utf-8")
        corrupt = DeterministicFileStateStore(corrupt_path)
        read = corrupt.read_workstream("WS-X")
        self.assertEqual(read.status, "CORRUPT")
        self.assertFalse(read.mutation_allowed)

    def test_canonical_effect_api_covers_required_actions(self):
        waiting = waiting_answer_effect_identity(
            project="UES", workstream_id="WS-A", session_id="s", waiting_activity_id="a"
        )
        correction = correction_packet_effect_identity(
            project="UES",
            workstream_id="WS-A",
            writer_session_id="w",
            reviewer_session_id="r",
            candidate_sha="c" * 40,
        )
        review = reviewer_dispatch_effect_identity(
            project="UES",
            workstream_id="WS-A",
            candidate_sha="c" * 40,
            reviewer_lineage="r",
            dispatch_target="target",
            re_review=True,
        )
        task = task_session_effect_identity(
            project="UES",
            workstream_id="WS-A",
            intent="create",
            task_kind="reviewer",
            lineage="r",
            authority_event_id="evt",
        )
        self.assertEqual(len({effect_operation_key(x) for x in (waiting, correction, review, task)}), 4)
        self.assertNotIn("digest", json.dumps(waiting.to_dict()).lower())


if __name__ == "__main__":
    unittest.main()
