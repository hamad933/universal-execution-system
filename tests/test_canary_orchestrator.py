from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.canary_orchestrator import (
    execute_waiting_answer_canary,
    reconcile_waiting_answer_operation,
)
from ues.idempotency import (
    canonical_request_digest,
    effect_operation_key,
    waiting_answer_effect_identity,
)
from ues.identity import canonical_lane_id
from ues.project_adapter import load_project_adapter
from ues.providers.base import AuthenticationError, WriteOutcomeUnknown
from ues.routing import WAITING_SAME_SESSION_CONTINUATION
from ues.state_store import (
    CanaryGrant,
    DeterministicFileStateStore,
    WorkstreamRuntimeRecord,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 24, 0, 20, tzinfo=UTC)
PROJECT = "CEP"
ROUTE = "PERSONAL:CEP"
WORKSTREAM = "W04"
LANE = canonical_lane_id(PROJECT, ROUTE, WORKSTREAM)
SESSION = "existing-writer-session"
ACTIVITY = "waiting-activity-1"
REPOSITORY = "hamad933/Cybersecurity-Education-Platform"
SOURCE = "sources/cep-source"
AUTHORITY = "canary-authority-1"
POLICY_EVIDENCE = "cep-policy-event-1"
PROMPT = "Use the governed handoff receipt boundary."
OBSERVED_START = {
    "provider_state": "AWAITING_USER_FEEDBACK",
    "waiting_activity_id": ACTIVITY,
}


class FakeJules:
    def __init__(self, *, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def send_message(
        self,
        session: str,
        prompt: str,
        *,
        expected_repository=None,
        expected_source=None,
    ):
        self.calls.append(
            {
                "session": session,
                "prompt": prompt,
                "expected_repository": expected_repository,
                "expected_source": expected_source,
            }
        )
        if self.outcome == "ambiguous":
            raise WriteOutcomeUnknown(
                "ambiguous",
                operation="jules.sendMessage",
                recovery={
                    "verdict": "AUTHORITATIVE_READ_UNAVAILABLE",
                    "safe_to_blind_retry": False,
                },
            )
        if self.outcome == "auth":
            raise AuthenticationError("denied", operation="jules.sendMessage")
        if self.outcome == "unexpected":
            raise RuntimeError("unexpected provider boundary failure")
        if self.outcome == "malformed":
            return {
                "outcome": "DELIVERED",
                "safe_to_blind_retry": False,
                "activity": None,
            }
        return {
            "schema_version": "0.5",
            "provider": "JULES",
            "operation": "sendMessage",
            "session": session,
            "outcome": "DELIVERED",
            "activity": {"name": "activities/delivered-1"},
            "safe_to_blind_retry": False,
            "Authorization": "Bearer must-not-persist",
        }


class CanaryOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.json"
        self.store = DeterministicFileStateStore(self.path, clock=lambda: T0)
        self.store.initialize()
        self.effect = waiting_answer_effect_identity(
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            session_id=SESSION,
            waiting_activity_id=ACTIVITY,
        )
        self._put_lane(mode="CANARY", with_grant=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _actors(
        self,
        *,
        proven: bool = True,
        session: str = SESSION,
        source_identity: str = SOURCE,
    ):
        return {
            "WRITER": {
                "provider": "jules",
                "session_id": session,
                "proof_status": "PROVEN_EXPLICIT" if proven else "PROPOSED_UNVERIFIED",
                "source_repository": REPOSITORY,
                "source_identity": source_identity,
                "evidence_id": "writer-binding-proof",
            },
            "REVIEWER": {
                "provider": "jules",
                "session_id": "reviewer-session",
                "proof_status": "PROVEN_EXPLICIT",
                "source_repository": REPOSITORY,
                "source_identity": SOURCE,
            },
        }

    def _grant(self, *, expected_start=None):
        return CanaryGrant(
            authority_event_id=AUTHORITY,
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            effect_type=self.effect.action,
            target=dict(self.effect.target),
            issued_at=(T0 - timedelta(minutes=1)).isoformat(),
            expires_at=(T0 + timedelta(minutes=10)).isoformat(),
            maximum_effect_count=1,
            expected_start=dict(OBSERVED_START if expected_start is None else expected_start),
        )

    def _put_lane(
        self,
        *,
        mode: str,
        with_grant: bool,
        proven: bool = True,
        session: str = SESSION,
        source_identity: str = SOURCE,
        grant_expected_start=None,
    ) -> None:
        read = self.store.read_workstream(LANE)
        expected = read.version if read.status == "OK" else 0
        record = WorkstreamRuntimeRecord(
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            activation_mode=mode,
            actor_bindings=self._actors(
                proven=proven,
                session=session,
                source_identity=source_identity,
            ),
            canary_grants=(
                [self._grant(expected_start=grant_expected_start)] if with_grant else []
            ),
        )
        self.store.compare_and_swap_workstream(LANE, expected, record)

    def _execute(self, jules: FakeJules, **overrides):
        args = {
            "lane_id": LANE,
            "project": PROJECT,
            "route": ROUTE,
            "workstream_id": WORKSTREAM,
            "session_id": SESSION,
            "waiting_activity_id": ACTIVITY,
            "expected_repository": REPOSITORY,
            "expected_source": SOURCE,
            "prompt": PROMPT,
            "project_auto_safe_actions": {WAITING_SAME_SESSION_CONTINUATION},
            "project_policy_evidence_id": POLICY_EVIDENCE,
            "canary_authority_event_id": AUTHORITY,
            "observed_start": OBSERVED_START,
            "owner": "canary-runner",
            "ttl_seconds": 60,
            "now": T0,
        }
        args.update(overrides)
        return execute_waiting_answer_canary(self.store, jules, **args)

    def test_noncanonical_lane_is_denied_before_state_or_provider_effect(self):
        jules = FakeJules()
        result = self._execute(jules, lane_id="CEP:PERSONAL:CEP:W04")
        self.assertEqual(result["decision"], "NONCANONICAL_LANE_ID")
        self.assertEqual(jules.calls, [])

    def test_policy_denial_happens_before_state_claim_or_provider_call(self):
        jules = FakeJules()
        result = self._execute(jules, project_auto_safe_actions=set())
        self.assertEqual(result["decision"], "PROJECT_ACTION_POLICY_DENIED")
        self.assertEqual(jules.calls, [])
        self.assertEqual(result["external_effects_dispatched"], 0)

    def test_raw_effect_label_is_not_project_action_authority(self):
        jules = FakeJules()
        result = self._execute(jules, project_auto_safe_actions={"waiting-answer"})
        self.assertEqual(result["decision"], "PROJECT_ACTION_POLICY_DENIED")
        self.assertEqual(jules.calls, [])

    def test_current_gs_and_cep_adapters_keep_live_canary_unreachable(self):
        for path in ("adapters/gs.json", "adapters/cep.json"):
            with self.subTest(path=path):
                adapter = load_project_adapter(Path(path))
                self.assertEqual(adapter.project_auto_safe_actions, ())
                jules = FakeJules()
                result = self._execute(
                    jules,
                    project_auto_safe_actions=adapter.project_auto_safe_actions,
                )
                self.assertEqual(result["decision"], "PROJECT_ACTION_POLICY_DENIED")
                self.assertEqual(jules.calls, [])

    def test_policy_evidence_identity_is_mandatory(self):
        jules = FakeJules()
        with self.assertRaises(ValueError):
            self._execute(jules, project_policy_evidence_id="")
        self.assertEqual(jules.calls, [])

    def test_shadow_runtime_never_calls_provider(self):
        self._put_lane(mode="SHADOW", with_grant=True)
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "SHADOW_MODE")
        self.assertEqual(jules.calls, [])

    def test_runtime_canary_without_exact_grant_never_calls_provider(self):
        self._put_lane(mode="CANARY", with_grant=False)
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "CANARY_GRANT_NOT_FOUND")
        self.assertEqual(jules.calls, [])

    def test_grant_must_bind_waiting_provider_state_and_activity(self):
        self._put_lane(
            mode="CANARY",
            with_grant=True,
            grant_expected_start={"provider_state": "AWAITING_USER_FEEDBACK"},
        )
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "CANARY_GRANT_WAITING_ACTIVITY_BINDING_REQUIRED")
        self.assertEqual(jules.calls, [])

    def test_observed_start_must_still_be_same_waiting_activity(self):
        jules = FakeJules()
        result = self._execute(
            jules,
            observed_start={
                "provider_state": "AWAITING_USER_FEEDBACK",
                "waiting_activity_id": "different-activity",
            },
        )
        self.assertEqual(result["decision"], "OBSERVED_WAITING_ACTIVITY_MISMATCH")
        self.assertEqual(jules.calls, [])

    def test_observed_provider_state_must_be_awaiting_feedback(self):
        jules = FakeJules()
        result = self._execute(
            jules,
            observed_start={
                "provider_state": "IN_PROGRESS",
                "waiting_activity_id": ACTIVITY,
            },
        )
        self.assertEqual(result["decision"], "OBSERVED_PROVIDER_STATE_NOT_AWAITING_USER_FEEDBACK")
        self.assertEqual(jules.calls, [])

    def test_unproven_writer_binding_never_calls_provider(self):
        self._put_lane(mode="CANARY", with_grant=True, proven=False)
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "WRITER_BINDING_NOT_EXPLICITLY_PROVEN")
        self.assertEqual(jules.calls, [])

    def test_wrong_writer_session_never_calls_provider(self):
        self._put_lane(mode="CANARY", with_grant=True, session="other-session")
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "WRITER_SESSION_MISMATCH")
        self.assertEqual(jules.calls, [])

    def test_wrong_source_identity_never_calls_provider(self):
        self._put_lane(mode="CANARY", with_grant=True, source_identity="sources/other")
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "WRITER_SOURCE_IDENTITY_MISMATCH")
        self.assertEqual(jules.calls, [])

    def test_matching_one_shot_grant_claims_before_one_send_and_confirms(self):
        jules = FakeJules()
        result = self._execute(jules)
        self.assertEqual(result["decision"], "CANARY_EFFECT_CONFIRMED")
        self.assertEqual(result["operation_state"], "CONFIRMED")
        self.assertEqual(len(jules.calls), 1)
        self.assertEqual(jules.calls[0]["expected_repository"], REPOSITORY)
        self.assertEqual(jules.calls[0]["expected_source"], SOURCE)
        self.assertEqual(result["tasks_or_sessions_created"], 0)
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertFalse(result["live_authority_granted_by_code"])

        op = self.store.read_operation(result["operation_key"])
        self.assertEqual(op.record.state, "CONFIRMED")
        self.assertEqual(op.record.receipt["project_policy_evidence_id"], POLICY_EVIDENCE)
        self.assertEqual(op.record.receipt["canary_authority_event_id"], AUTHORITY)
        lane = self.store.read_workstream(LANE).record
        self.assertIsNone(lane.lease)
        self.assertTrue(lane.canary_grants[0].consumed)
        self.assertNotIn("must-not-persist", str(op.record.to_dict()))

    def test_identical_replay_never_sends_again(self):
        jules = FakeJules()
        first = self._execute(jules)
        second = self._execute(jules)
        self.assertEqual(first["decision"], "CANARY_EFFECT_CONFIRMED")
        self.assertEqual(second["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertEqual(len(jules.calls), 1)

    def test_changed_prompt_same_effect_is_collision_and_never_resends(self):
        jules = FakeJules()
        first = self._execute(jules)
        second = self._execute(jules, prompt="changed answer")
        self.assertEqual(first["decision"], "CANARY_EFFECT_CONFIRMED")
        self.assertEqual(second["decision"], "OPERATION_ID_COLLISION")
        self.assertEqual(len(jules.calls), 1)

    def test_ambiguous_provider_write_becomes_unknown_and_never_blind_retries(self):
        jules = FakeJules(outcome="ambiguous")
        first = self._execute(jules)
        self.assertEqual(first["decision"], "WRITE_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED")
        self.assertEqual(first["operation_state"], "UNKNOWN")
        self.assertEqual(first["stop_gate"], "AUTHORITATIVE_READBACK_REQUIRED")
        self.assertFalse(first["safe_to_blind_retry"])
        self.assertEqual(len(jules.calls), 1)

        second = self._execute(jules)
        self.assertEqual(second["decision"], "RECONCILE_REQUIRED")
        self.assertEqual(len(jules.calls), 1)

    def test_provider_error_after_claim_is_conservatively_unknown(self):
        jules = FakeJules(outcome="auth")
        result = self._execute(jules)
        self.assertEqual(result["decision"], "PROVIDER_ERROR_RECONCILIATION_REQUIRED")
        self.assertEqual(result["operation_state"], "UNKNOWN")
        self.assertEqual(len(jules.calls), 1)

    def test_unexpected_provider_exception_is_conservatively_unknown(self):
        jules = FakeJules(outcome="unexpected")
        result = self._execute(jules)
        self.assertEqual(
            result["decision"],
            "UNEXPECTED_PROVIDER_OUTCOME_RECONCILIATION_REQUIRED",
        )
        self.assertEqual(result["operation_state"], "UNKNOWN")
        self.assertEqual(len(jules.calls), 1)
        self.assertNotIn("unexpected provider boundary failure", str(result))

    def test_malformed_success_receipt_does_not_become_confirmed(self):
        jules = FakeJules(outcome="malformed")
        result = self._execute(jules)
        self.assertEqual(result["decision"], "PROVIDER_RECEIPT_UNPROVEN_RECONCILIATION_REQUIRED")
        self.assertEqual(result["operation_state"], "UNKNOWN")
        self.assertEqual(len(jules.calls), 1)

    def test_later_authoritative_readback_resolves_unknown_without_provider_write(self):
        jules = FakeJules(outcome="ambiguous")
        first = self._execute(jules)
        operation_key = first["operation_key"]
        reconciled = reconcile_waiting_answer_operation(
            self.store,
            lane_id=LANE,
            operation_key=operation_key,
            observed=True,
            evidence={"activity": "activities/confirmed-later"},
            now=T0 + timedelta(seconds=30),
        )
        self.assertEqual(reconciled["operation_state"], "CONFIRMED")
        self.assertFalse(reconciled["provider_write_attempted"])
        self.assertEqual(len(jules.calls), 1)

    def test_effect_identity_is_session_activity_bound_while_prompt_is_payload_evidence(self):
        operation_key = effect_operation_key(self.effect)
        self.assertEqual(
            operation_key,
            effect_operation_key(
                waiting_answer_effect_identity(
                    lane_id=LANE,
                    project=PROJECT,
                    route=ROUTE,
                    workstream_id=WORKSTREAM,
                    session_id=SESSION,
                    waiting_activity_id=ACTIVITY,
                )
            ),
        )
        self.assertNotEqual(
            canonical_request_digest({"prompt": PROMPT}),
            canonical_request_digest({"prompt": "changed"}),
        )


if __name__ == "__main__":
    unittest.main()
