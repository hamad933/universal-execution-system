from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.canary_orchestration import execute_jules_waiting_canary
from ues.idempotency import canonical_effect_identity, effect_operation_key, waiting_answer_effect_identity
from ues.identity import canonical_lane_id
from ues.project_adapter import load_project_adapter
from ues.providers.base import ProtocolError, WriteOutcomeUnknown
from ues.routing import WAITING_SAME_SESSION_CONTINUATION
from ues.state_store import CanaryGrant, DeterministicFileStateStore, WorkstreamRuntimeRecord

UTC = timezone.utc
T0 = datetime(2026, 8, 24, 0, 30, tzinfo=UTC)
PROJECT = "SYNTH"
ROUTE = "INTERNAL:SYNTH"
WORKSTREAM = "W01"
LANE = canonical_lane_id(PROJECT, ROUTE, WORKSTREAM)
POLICY = {WAITING_SAME_SESSION_CONTINUATION}


class FakeJulesClient:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []

    def send_message(self, session, prompt, *, expected_repository=None, expected_source=None):
        self.calls.append(
            {
                "session": session,
                "prompt": prompt,
                "expected_repository": expected_repository,
                "expected_source": expected_source,
            }
        )
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior()
        return self.behavior or {
            "provider": "JULES",
            "operation": "sendMessage",
            "session": f"sessions/{session}",
            "outcome": "DELIVERED",
            "activity": "activities/new",
            "safe_to_blind_retry": False,
        }


class CanaryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.tmp.name) / "state.json", clock=lambda: T0)
        self.store.initialize()
        self.effect = waiting_answer_effect_identity(
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            session_id="session-1",
            waiting_activity_id="activity-1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def put_runtime(self, *, mode="CANARY", grant=True, effect=None):
        effect = effect or self.effect
        grants = []
        if grant:
            grants.append(
                CanaryGrant(
                    authority_event_id="canary-1",
                    lane_id=effect.lane_id,
                    project=effect.project,
                    route=effect.route,
                    workstream_id=effect.workstream_id,
                    effect_type=effect.action,
                    target=dict(effect.target),
                    issued_at=(T0 - timedelta(minutes=1)).isoformat(),
                    expires_at=(T0 + timedelta(minutes=5)).isoformat(),
                    maximum_effect_count=1,
                    expected_start={"provider_state": "AWAITING_USER_FEEDBACK"},
                )
            )
        read = self.store.read_workstream(effect.lane_id)
        expected = read.version if read.status == "OK" else 0
        self.store.compare_and_swap_workstream(
            effect.lane_id,
            expected,
            WorkstreamRuntimeRecord(
                lane_id=effect.lane_id,
                project=effect.project,
                route=effect.route,
                workstream_id=effect.workstream_id,
                activation_mode=mode,
                canary_grants=grants,
            ),
        )

    def execute(self, client, *, effect=None, prompt="bounded answer", policy=POLICY, source="sources/src-1"):
        return execute_jules_waiting_canary(
            store=self.store,
            client=client,
            effect=effect or self.effect,
            prompt=prompt,
            project_auto_safe_actions=policy,
            expected_repository="owner/repo",
            expected_source=source,
            canary_authority_event_id="canary-1",
            observed_start={"provider_state": "AWAITING_USER_FEEDBACK"},
            owner="runner-1",
            ttl_seconds=60,
            now=T0,
        )

    def test_current_gs_and_cep_adapters_do_not_authorize_canary_effect(self):
        for path in ("adapters/gs.json", "adapters/cep.json"):
            with self.subTest(path=path):
                adapter = load_project_adapter(Path(path))
                self.assertEqual(adapter.project_auto_safe_actions, ())
                self.put_runtime()
                client = FakeJulesClient()
                result = self.execute(client, policy=adapter.project_auto_safe_actions)
                self.assertEqual(result["decision"], "PROJECT_ACTION_POLICY_DENIED")
                self.assertEqual(client.calls, [])

    def test_shadow_mode_never_calls_provider_even_with_policy(self):
        self.put_runtime(mode="SHADOW", grant=False)
        client = FakeJulesClient()
        result = self.execute(client)
        self.assertEqual(result["decision"], "SHADOW_MODE")
        self.assertFalse(result["provider_call_invoked"])
        self.assertEqual(client.calls, [])

    def test_canary_mode_without_exact_grant_never_calls_provider(self):
        self.put_runtime(grant=False)
        client = FakeJulesClient()
        result = self.execute(client)
        self.assertEqual(result["decision"], "CANARY_GRANT_NOT_FOUND")
        self.assertEqual(client.calls, [])

    def test_missing_exact_source_fails_before_claim_or_provider(self):
        self.put_runtime()
        client = FakeJulesClient()
        result = self.execute(client, source="")
        self.assertEqual(result["decision"], "EXACT_JULES_SOURCE_REQUIRED")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.read_operation(effect_operation_key(self.effect)).status, "MISSING")

    def test_wrong_effect_type_fails_before_provider(self):
        wrong = canonical_effect_identity(
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            action="reviewer-dispatch",
            target={"provider": "jules", "session_id": "session-1", "waiting_activity_id": "activity-1"},
        )
        self.put_runtime(effect=wrong)
        client = FakeJulesClient()
        result = self.execute(client, effect=wrong)
        self.assertEqual(result["decision"], "WAITING_EFFECT_ACTION_REQUIRED")
        self.assertEqual(client.calls, [])

    def test_exact_policy_and_one_shot_grant_confirm_once(self):
        self.put_runtime()
        client = FakeJulesClient()
        result = self.execute(client)
        self.assertEqual(result["decision"], "CONFIRMED")
        self.assertTrue(result["provider_confirmed"])
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["session"], "session-1")
        self.assertEqual(call["expected_repository"], "owner/repo")
        self.assertEqual(call["expected_source"], "sources/src-1")

        operation = self.store.read_operation(effect_operation_key(self.effect)).record
        self.assertEqual(operation.state, "CONFIRMED")
        runtime = self.store.read_workstream(LANE).record
        self.assertIsNone(runtime.lease)
        self.assertEqual(runtime.canary_grants[0].consumed_count, 1)

        second = self.execute(client)
        self.assertEqual(second["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertEqual(len(client.calls), 1)

    def test_same_effect_changed_payload_is_collision_without_second_send(self):
        self.put_runtime()
        client = FakeJulesClient()
        self.assertEqual(self.execute(client, prompt="first")["decision"], "CONFIRMED")
        changed = self.execute(client, prompt="changed")
        self.assertEqual(changed["decision"], "OPERATION_ID_COLLISION")
        self.assertEqual(len(client.calls), 1)

    def test_ambiguous_provider_write_is_durable_unknown_and_never_resends(self):
        self.put_runtime()
        unknown = WriteOutcomeUnknown(
            "post-write readback unavailable",
            operation="jules.sendMessage",
            recovery={
                "verdict": "AUTHORITATIVE_READ_UNAVAILABLE",
                "safe_to_blind_retry": False,
            },
        )
        client = FakeJulesClient(unknown)
        result = self.execute(client)
        self.assertEqual(result["decision"], "WRITE_OUTCOME_UNKNOWN_RECONCILE_REQUIRED")
        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(len(client.calls), 1)
        operation = self.store.read_operation(effect_operation_key(self.effect)).record
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertTrue(operation.reconciliation_required)

        retry = self.execute(client)
        self.assertEqual(retry["decision"], "RECONCILE_REQUIRED")
        self.assertEqual(len(client.calls), 1)

    def test_definitive_provider_failure_is_terminal_and_not_retried(self):
        self.put_runtime()
        client = FakeJulesClient(ProtocolError("binding changed", operation="jules.sendMessage"))
        result = self.execute(client)
        self.assertEqual(result["decision"], "DEFINITIVE_PROVIDER_FAILURE")
        self.assertEqual(result["operation_state"], "REJECTED")
        self.assertTrue(result["lease_released"])
        self.assertEqual(len(client.calls), 1)
        operation = self.store.read_operation(effect_operation_key(self.effect)).record
        self.assertEqual(operation.state, "REJECTED")
        self.assertIsNone(self.store.read_workstream(LANE).record.lease)

        retry = self.execute(client)
        self.assertEqual(retry["decision"], "TERMINAL_REPLAY_REJECTED")
        self.assertEqual(len(client.calls), 1)

    def test_unexpected_provider_exception_is_unknown_not_retryable(self):
        self.put_runtime()
        client = FakeJulesClient(RuntimeError("unexpected boundary failure"))
        result = self.execute(client)
        self.assertEqual(result["decision"], "UNEXPECTED_PROVIDER_OUTCOME_RECONCILE_REQUIRED")
        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(self.store.read_operation(effect_operation_key(self.effect)).record.state, "UNKNOWN")
        self.assertEqual(len(client.calls), 1)

    def test_provider_receipt_is_sanitized_before_durable_persistence(self):
        self.put_runtime()
        client = FakeJulesClient(
            {
                "provider": "JULES",
                "outcome": "DELIVERED",
                "activity": "activities/new",
                "nested": {"api_key": "do-not-persist-this"},
            }
        )
        self.assertEqual(self.execute(client)["decision"], "CONFIRMED")
        serialized = json.dumps(self.store.read_operation(effect_operation_key(self.effect)).record.to_dict())
        self.assertNotIn("do-not-persist-this", serialized)

    def test_result_contract_never_claims_task_creation_or_blind_retry(self):
        self.put_runtime()
        result = self.execute(FakeJulesClient())
        self.assertFalse(result["new_task_or_session_created"])
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertFalse(result["live_authority_granted_by_code"])


if __name__ == "__main__":
    unittest.main()
