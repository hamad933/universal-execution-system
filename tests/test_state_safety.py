import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.idempotency import (
    canonical_effect_identity,
    canonical_request_digest,
    correction_packet_effect_identity,
    effect_operation_key,
    reviewer_dispatch_effect_identity,
    waiting_answer_effect_identity,
    waiting_answer_operation_key,
)
from ues.operation_records import sanitize_receipt
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
T0 = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
GS_LANE = "lane:GS:PERSONAL-GS:W01"
CEP_LANE = "lane:CEP:PERSONAL-CEP:W01"
GS_ROUTE = "PERSONAL:GS"
CEP_ROUTE = "PERSONAL:CEP"


def actor_bindings(prefix="gs"):
    return {
        "WRITER": {
            "provider": "jules",
            "session_id": f"{prefix}-writer",
            "proof_status": "PROVEN_EXPLICIT",
            "source_repository": "owner/repo",
            "evidence_id": f"{prefix}-writer-binding",
        },
        "REVIEWER": {
            "provider": "jules",
            "session_id": f"{prefix}-reviewer",
            "proof_status": "PROVEN_EXPLICIT",
            "source_repository": "owner/repo",
            "evidence_id": f"{prefix}-reviewer-binding",
        },
    }


class StateSafetyR2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.json"
        self.store = DeterministicFileStateStore(self.path, clock=lambda: T0)
        self.store.initialize()
        self._put_lane(GS_LANE, "GS", GS_ROUTE, "W01", mode="CANARY")

    def tearDown(self):
        self.tmp.cleanup()

    def _put_lane(
        self,
        lane_id,
        project,
        route,
        workstream,
        *,
        mode="CANARY",
        grants=None,
        actors=None,
    ):
        read = self.store.read_workstream(lane_id)
        expected = read.version if read.status == "OK" else 0
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream,
            activation_mode=mode,
            actor_bindings=actors if actors is not None else actor_bindings(project.lower()),
            canary_grants=list(grants or []),
        )
        return self.store.compare_and_swap_workstream(lane_id, expected, record)

    def _waiting(self, *, lane_id=GS_LANE, project="GS", route=GS_ROUTE, session="s1", activity="a1"):
        return waiting_answer_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id="W01",
            session_id=session,
            waiting_activity_id=activity,
        )

    def _grant(self, effect, *, event="grant-1", consumed=0, expires=None):
        return CanaryGrant(
            authority_event_id=event,
            lane_id=effect.lane_id,
            project=effect.project,
            route=effect.route,
            workstream_id=effect.workstream_id,
            effect_type=effect.action,
            target=dict(effect.target),
            issued_at=(T0 - timedelta(minutes=1)).isoformat(),
            expires_at=(expires or (T0 + timedelta(minutes=10))).isoformat(),
            maximum_effect_count=1,
            consumed_count=consumed,
            consumed_at=(T0 - timedelta(seconds=1)).isoformat() if consumed else None,
            consumed_operation_keys=["prior-op"] if consumed else [],
        )

    def _set_grant(self, effect, **kwargs):
        grant = self._grant(effect, **kwargs)
        project = effect.project
        self._put_lane(
            effect.lane_id,
            project,
            effect.route,
            effect.workstream_id,
            mode="CANARY",
            grants=[grant],
        )
        return grant

    def _claim(self, effect, payload, *, owner="runner-1", now=T0, authorization=None):
        return claim_operation(
            self.store,
            lane_id=effect.lane_id,
            owner=owner,
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=canonical_request_digest(payload),
            ttl_seconds=60,
            effect_identity=effect,
            authorization=authorization,
            now=now,
        )

    def test_lane_id_is_mandatory_and_audit_identity_persists(self):
        read = self.store.read_workstream(GS_LANE)
        self.assertEqual(read.status, "OK")
        self.assertEqual(read.record.lane_id, GS_LANE)
        self.assertEqual(read.record.project, "GS")
        self.assertEqual(read.record.route, GS_ROUTE)
        self.assertEqual(read.record.workstream_id, "W01")
        with self.assertRaises(ValueError):
            WorkstreamRuntimeRecord(
                lane_id="",
                project="GS",
                route=GS_ROUTE,
                workstream_id="W01",
            )

    def test_same_bare_workstream_is_independent_across_canonical_lane_ids(self):
        self._put_lane(CEP_LANE, "CEP", CEP_ROUTE, "W01", actors=actor_bindings("cep"))
        gs = self.store.read_workstream(GS_LANE)
        cep = self.store.read_workstream(CEP_LANE)
        self.assertEqual(gs.record.workstream_id, cep.record.workstream_id)
        self.assertNotEqual(gs.record.lane_id, cep.record.lane_id)
        self.store.acquire_lease(GS_LANE, "gs-runner", "op-gs", 60, now=T0)
        acquired = self.store.acquire_lease(CEP_LANE, "cep-runner", "op-cep", 60, now=T0)
        self.assertEqual(acquired.lease.operation_key, "op-cep")

    def test_effect_key_binds_lane_and_route(self):
        gs = canonical_effect_identity(
            lane_id=GS_LANE,
            project="GS",
            route=GS_ROUTE,
            workstream_id="W01",
            action="waiting-answer",
            target={"session_id": "s1", "activity_id": "a1"},
        )
        changed_route = canonical_effect_identity(
            lane_id="lane:GS:OTHER:W01",
            project="GS",
            route="PERSONAL:OTHER",
            workstream_id="W01",
            action="waiting-answer",
            target={"session_id": "s1", "activity_id": "a1"},
        )
        self.assertNotEqual(effect_operation_key(gs), effect_operation_key(changed_route))

    def test_role_specific_actor_bindings_survive_serialization_and_are_sanitized(self):
        actors = actor_bindings("safe")
        actors["WRITER"]["Authorization"] = "Bearer super-secret"
        self._put_lane(GS_LANE, "GS", GS_ROUTE, "W01", actors=actors)
        runner_b = DeterministicFileStateStore(self.path, clock=lambda: T0)
        restored = runner_b.read_workstream(GS_LANE).record
        self.assertEqual(restored.actor_bindings["WRITER"]["session_id"], "safe-writer")
        self.assertEqual(restored.actor_bindings["REVIEWER"]["session_id"], "safe-reviewer")
        self.assertNotIn("super-secret", json.dumps(restored.to_dict()))

    def test_same_waiting_activity_changed_answer_is_collision(self):
        effect = self._waiting()
        self._set_grant(effect)
        first_digest = canonical_request_digest({"answer": "first"})
        second_digest = canonical_request_digest({"answer": "changed"})
        key1 = waiting_answer_operation_key(
            lane_id=GS_LANE,
            project="GS",
            route=GS_ROUTE,
            workstream_id="W01",
            session_id="s1",
            waiting_activity_id="a1",
            answer_digest=first_digest,
        )
        key2 = waiting_answer_operation_key(
            lane_id=GS_LANE,
            project="GS",
            route=GS_ROUTE,
            workstream_id="W01",
            session_id="s1",
            waiting_activity_id="a1",
            answer_digest=second_digest,
        )
        self.assertEqual(key1, key2)
        self.assertEqual(self._claim(effect, {"answer": "first"})["decision"], "CLAIMED")
        second = self._claim(effect, {"answer": "changed"}, owner="runner-2")
        self.assertEqual(second["decision"], "OPERATION_ID_COLLISION")
        self.assertFalse(second["mutation_allowed"])

    def test_wrong_lane_effect_is_denied_before_canary(self):
        granted = self._waiting()
        self._set_grant(granted)
        wrong = self._waiting(
            lane_id=CEP_LANE,
            project="CEP",
            route=CEP_ROUTE,
        )
        result = claim_operation(
            self.store,
            lane_id=GS_LANE,
            owner="runner",
            operation_key=effect_operation_key(wrong),
            action=wrong.action,
            request_digest=canonical_request_digest({"answer": "x"}),
            ttl_seconds=60,
            effect_identity=wrong,
            now=T0,
        )
        self.assertEqual(result["decision"], "EFFECT_IDENTITY_LANE_MISMATCH")

    def test_canary_without_exact_grant_is_denied(self):
        effect = self._waiting()
        result = self._claim(effect, {"answer": "x"})
        self.assertEqual(result["decision"], "CANARY_GRANT_NOT_FOUND")

    def test_expired_and_consumed_canary_grants_are_denied(self):
        effect = self._waiting()
        self._set_grant(effect, expires=T0 - timedelta(seconds=1))
        self.assertEqual(self._claim(effect, {"answer": "x"})["decision"], "CANARY_GRANT_EXPIRED")
        self._set_grant(effect, consumed=1)
        self.assertEqual(self._claim(effect, {"answer": "x"})["decision"], "CANARY_GRANT_CONSUMED")

    def test_matching_canary_is_consumed_before_mutation_allowed(self):
        effect = reviewer_dispatch_effect_identity(
            lane_id=GS_LANE,
            project="GS",
            route=GS_ROUTE,
            workstream_id="W01",
            candidate_sha="a" * 40,
            reviewer_lineage="reviewer",
            dispatch_target="reviewer-session",
        )
        self._set_grant(effect)
        result = self._claim(effect, {"dispatch": "reviewer"})
        self.assertEqual(result["decision"], "CLAIMED")
        self.assertTrue(result["mutation_allowed"])
        state = self.store.read_workstream(GS_LANE).record
        self.assertEqual(state.canary_grants[0].consumed_count, 1)
        op = self.store.read_operation(effect_operation_key(effect)).record
        self.assertEqual(op.state, "IN_FLIGHT")
        self.assertEqual(op.lane_id, GS_LANE)

    def test_active_auto_safe_requires_exact_action_authority(self):
        effect = correction_packet_effect_identity(
            lane_id=GS_LANE,
            project="GS",
            route=GS_ROUTE,
            workstream_id="W01",
            writer_session_id="writer",
            reviewer_session_id="reviewer",
            candidate_sha="b" * 40,
        )
        self._put_lane(GS_LANE, "GS", GS_ROUTE, "W01", mode="ACTIVE_AUTO_SAFE")
        self.assertEqual(self._claim(effect, {"findings": ["x"]})["decision"], "ACTION_AUTHORITY_REQUIRED")
        auth = MutationAuthorization(
            effect_identity=effect,
            authority_event_id="policy-1",
            project_policy_authorized=True,
            exact_binding_proven=True,
            evidence_verified=True,
            expires_at=(T0 + timedelta(minutes=2)).isoformat(),
        )
        allowed = self._claim(effect, {"findings": ["x"]}, authorization=auth)
        self.assertEqual(allowed["decision"], "CLAIMED")

    def test_restart_after_send_requires_authoritative_readback(self):
        effect = self._waiting(activity="restart-a")
        self._set_grant(effect)
        payload = {"answer": "bounded"}
        self.assertEqual(self._claim(effect, payload)["decision"], "CLAIMED")

        runner_b = DeterministicFileStateStore(self.path, clock=lambda: T0 + timedelta(seconds=90))
        blocked = claim_operation(
            runner_b,
            lane_id=GS_LANE,
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
            lane_id=GS_LANE,
            operation_key=effect_operation_key(effect),
            observed=False,
            evidence={"session_id": "s1", "activity_present": False},
            now=T0 + timedelta(seconds=91),
        )
        self.assertEqual(readback.record.state, "RECONCILED_NOT_OBSERVED")
        retry = claim_operation(
            runner_b,
            lane_id=GS_LANE,
            owner="runner-b",
            operation_key=effect_operation_key(effect),
            action=effect.action,
            request_digest=canonical_request_digest(payload),
            ttl_seconds=60,
            effect_identity=effect,
            now=T0 + timedelta(seconds=92),
        )
        self.assertEqual(retry["decision"], "CANARY_GRANT_CONSUMED")

    def test_unknown_write_remains_blocked_until_readback(self):
        effect = self._waiting(activity="unknown-a")
        self._set_grant(effect)
        payload = {"answer": "bounded"}
        self._claim(effect, payload)
        record_unknown_write(
            self.store,
            lane_id=GS_LANE,
            operation_key=effect_operation_key(effect),
            result={"transport": "timeout"},
            now=T0,
        )
        retry = self._claim(effect, payload, owner="runner-2", now=T0 + timedelta(seconds=61))
        self.assertEqual(retry["decision"], "RECONCILE_REQUIRED")
        unresolved = record_authoritative_readback(
            self.store,
            lane_id=GS_LANE,
            operation_key=effect_operation_key(effect),
            observed=None,
            evidence={"read_complete": False},
            now=T0 + timedelta(seconds=62),
        )
        self.assertEqual(unresolved.record.state, "UNKNOWN")

    def test_lane_local_lease_collision_and_stale_recovery(self):
        self.store.acquire_lease(GS_LANE, "runner-1", "op-1", 10, now=T0)
        with self.assertRaises(LeaseCollision):
            self.store.acquire_lease(GS_LANE, "runner-2", "op-2", 60, now=T0)
        recovered = self.store.acquire_lease(
            GS_LANE,
            "runner-2",
            "op-2",
            60,
            now=T0 + timedelta(seconds=11),
        )
        self.assertTrue(recovered.stale_recovered)

    def test_state_version_conflict_fails_closed(self):
        read = self.store.read_workstream(GS_LANE)
        self.store.compare_and_swap_workstream(GS_LANE, read.version, read.record)
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(GS_LANE, read.version, read.record)

    def test_local_backend_remains_explicitly_not_production_ready(self):
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
        self.assertTrue(production_state_store_assessment(conformant)["ready_for_cross_run_production"])

    def test_missing_or_corrupt_state_is_shadow(self):
        missing = DeterministicFileStateStore(Path(self.tmp.name) / "missing.json")
        read = missing.read_workstream(GS_LANE)
        self.assertEqual(read.effective_activation_mode, "SHADOW")
        self.assertFalse(read.mutation_allowed)
        corrupt_path = Path(self.tmp.name) / "corrupt.json"
        corrupt_path.write_text("not-json", encoding="utf-8")
        corrupt = DeterministicFileStateStore(corrupt_path)
        read = corrupt.read_workstream(GS_LANE)
        self.assertEqual(read.status, "CORRUPT")
        self.assertEqual(read.effective_activation_mode, "SHADOW")

    def test_receipt_sanitization_still_removes_secrets(self):
        safe = sanitize_receipt(
            {
                "lane_id": GS_LANE,
                "session_id": "session-safe",
                "Authorization": "Bearer super-secret-token",
                "nested": {"api_key": "abcdef"},
            }
        )
        serialized = json.dumps(safe)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("abcdef", serialized)
        self.assertIn("session-safe", serialized)


if __name__ == "__main__":
    unittest.main()
