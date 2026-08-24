from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ues.initial_lineage_effects import execute_initial_lineage_generation
from ues.lineage_registry import lineage_lane_id
from ues.state_store import DeterministicFileStateStore
from ues.task_budget_accounting import read_budget_accounting


class FakeInitialClient:
    def __init__(self, before_create=None) -> None:
        self.create_calls = 0
        self.before_create = before_create

    def create_session(self, *, prompt, title, source, starting_branch, expected_repository):
        self.create_calls += 1
        if self.before_create is not None:
            self.before_create()
        return {
            "session": f"sessions/initial-{self.create_calls}",
            "repository": expected_repository,
            "starting_branch": starting_branch,
            "state": "QUEUED",
        }


class ExplodingInitialClient(FakeInitialClient):
    def create_session(self, **kwargs):
        self.create_calls += 1
        if self.before_create is not None:
            self.before_create()
        raise RuntimeError("provider transport ambiguity")


class InitialLineageEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
        self.adapter = {
            "project": "RP01",
            "route": "RP01",
            "repository": "hamad933/Bayt-Style",
            "authority_transport": {"controller_actor_allowlist": ["hamad933"]},
        }
        self.task_spec = {
            "objective": "Implement only the governed RP01 workstream",
            "exact_baseline": "main@" + "a" * 40,
            "write_scope": ["src/**"],
            "prohibited_scope": ["deploy/**"],
            "validation": ["unit"],
            "evidence": ["exact-head-ci"],
            "handoff": "Return SHA, changed paths, tests and Draft PR",
            "stop_gate": "DRAFT_PR_AND_EXACT_HEAD_CI",
        }
        self.authority = {
            "source": "DRIVE_CURRENT_STATE",
            "source_id": "drive:rp01-current",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH-001",
            "expires_at": "2026-08-24T22:00:00Z",
            "generation_policy": {
                "authorized_initial_lineages": {
                    "W11:WRITER": {
                        "authorized": True,
                        "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                        "task_spec": dict(self.task_spec),
                    }
                }
            },
        }
        self.policy = {
            "necessary_generation_authorized": True,
            "generation_effect_authorized": True,
            "generation_budget_safe": True,
            "budget": {"hard_ceiling_reached": False},
            "provenance": {"source": "DRIVE_CURRENT_STATE", "authority_event_id": "RP01-AUTH-001"},
        }
        self.lane_id = lineage_lane_id("RP01", "RP01", "W11", "WRITER")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, client, **overrides):
        values = dict(
            store=self.store,
            client=client,
            adapter=self.adapter,
            authority=self.authority,
            transport_actor="hamad933",
            authority_now=self.now,
            current_policy=self.policy,
            project="RP01",
            route="RP01",
            workstream="W11",
            role="WRITER",
            task_spec=self.task_spec,
            prompt="Implement W11 under the exact governed task specification.",
            title="RP01 W11 Writer G1",
            source_name="sources/github/hamad933/Bayt-Style",
            starting_branch="main",
            repository="hamad933/Bayt-Style",
            candidate_sha="a" * 40,
            active_duplicate_absent=True,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
        )
        values.update(overrides)
        return values

    def assert_shadow(self):
        read = self.store.read_workstream(self.lane_id)
        self.assertEqual(read.status, "OK")
        self.assertIsNotNone(read.record)
        assert read.record is not None
        self.assertEqual(read.record.activation_mode, "SHADOW")
        self.assertFalse(bool((read.record.authority_provenance or {}).get("effect_scope_active")))

    def test_exact_fresh_authority_creates_generation_one_once_and_accounts_it(self):
        def before_create():
            read = self.store.read_workstream(self.lane_id)
            self.assertEqual(read.status, "OK")
            assert read.record is not None
            pending = (read.record.evidence_bindings or {}).get("pending_initial_lineage_transition")
            self.assertIsInstance(pending, dict)
            self.assertEqual(pending["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
            self.assertFalse(pending["safe_to_blind_retry"])

        client = FakeInitialClient(before_create=before_create)
        first = execute_initial_lineage_generation(**self.args(client))
        self.assertEqual(first["decision"], "INITIAL_LOGICAL_LINEAGE_GENERATION_CONFIRMED")
        self.assertEqual(first["generation"], 1)
        self.assertEqual(client.create_calls, 1)
        self.assert_shadow()

        read = self.store.read_workstream(self.lane_id)
        assert read.record is not None
        evidence = read.record.evidence_bindings or {}
        self.assertEqual(evidence["generation"], 1)
        self.assertEqual(evidence["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
        self.assertNotIn("pending_initial_lineage_transition", evidence)
        self.assertEqual((read.record.authority_provenance or {})["scope"], "INITIAL_LOGICAL_LINEAGE_CREATE")
        self.assertFalse(evidence["raw_session_id_persisted"])

        budget = read_budget_accounting(self.store, project="RP01", route="RP01")
        self.assertEqual(budget["ues_confirmed_generation_count"], 1)

        second = execute_initial_lineage_generation(**self.args(client))
        self.assertEqual(second["decision"], "IDEMPOTENT_INITIAL_LINEAGE_ALREADY_BOUND")
        self.assertEqual(client.create_calls, 1)
        self.assert_shadow()
        budget = read_budget_accounting(self.store, project="RP01", route="RP01")
        self.assertEqual(budget["ues_confirmed_generation_count"], 1)

    def test_raw_stale_or_wrong_actor_authority_never_reaches_state_or_provider(self):
        client = FakeInitialClient()
        stale = execute_initial_lineage_generation(
            **self.args(client, authority_now=datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc))
        )
        self.assertEqual(stale["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")
        wrong_actor = execute_initial_lineage_generation(**self.args(client, transport_actor="someone-else"))
        self.assertEqual(wrong_actor["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(self.store.read_workstream(self.lane_id).status, "MISSING")

    def test_missing_mismatched_or_incomplete_task_contract_never_calls_provider(self):
        client = FakeInitialClient()
        no_authority = execute_initial_lineage_generation(**self.args(client, authority=None))
        self.assertEqual(no_authority["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")

        mismatched = execute_initial_lineage_generation(
            **self.args(client, task_spec={**self.task_spec, "objective": "different"})
        )
        self.assertEqual(mismatched["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")

        incomplete = dict(self.task_spec)
        incomplete.pop("stop_gate")
        authority = {**self.authority, "generation_policy": {"authorized_initial_lineages": {
            "W11:WRITER": {
                "authorized": True,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "task_spec": incomplete,
            }
        }}}
        malformed = execute_initial_lineage_generation(**self.args(client, authority=authority, task_spec=incomplete))
        self.assertEqual(malformed["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(self.store.read_workstream(self.lane_id).status, "MISSING")

    def test_adapter_identity_or_repository_mismatch_fails_before_provider(self):
        client = FakeInitialClient()
        wrong_repo_adapter = {**self.adapter, "repository": "other/repo"}
        result = execute_initial_lineage_generation(**self.args(client, adapter=wrong_repo_adapter))
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_CURRENT_AUTHORITY_OR_TASK_CONTRACT_REQUIRED")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(self.store.read_workstream(self.lane_id).status, "MISSING")

    def test_duplicate_or_policy_denial_blocks_before_provider(self):
        client = FakeInitialClient()
        duplicate = execute_initial_lineage_generation(**self.args(client, active_duplicate_absent=False))
        self.assertEqual(duplicate["decision"], "INITIAL_LINEAGE_BLOCKED")
        self.assertIn("ACTIVE_DUPLICATE_CHECK_REQUIRED", duplicate["transition"]["failures"])
        self.assertEqual(client.create_calls, 0)
        self.assert_shadow()

        denied_policy = {**self.policy, "generation_effect_authorized": False}
        denied = execute_initial_lineage_generation(**self.args(client, current_policy=denied_policy))
        self.assertEqual(denied["decision"], "INITIAL_LINEAGE_BLOCKED")
        self.assertIn("PROVIDER_GENERATION_EFFECT_NOT_AUTHORIZED", denied["transition"]["failures"])
        self.assertEqual(client.create_calls, 0)

    def test_unexpected_provider_result_becomes_unknown_and_second_call_does_not_retry(self):
        client = ExplodingInitialClient()
        first = execute_initial_lineage_generation(**self.args(client))
        self.assertEqual(first["decision"], "INITIAL_LINEAGE_UNEXPECTED_PROVIDER_ERROR_RECONCILIATION_REQUIRED")
        self.assertEqual(client.create_calls, 1)
        self.assert_shadow()

        lane = self.store.read_workstream(self.lane_id)
        assert lane.record is not None
        self.assertIsInstance(lane.record.unknown_write_state, dict)
        self.assertIsInstance((lane.record.evidence_bindings or {}).get("pending_initial_lineage_transition"), dict)

        second = execute_initial_lineage_generation(**self.args(client))
        self.assertEqual(second["decision"], "INITIAL_LINEAGE_BLOCKED")
        self.assertIn("UNKNOWN_WRITE_RECONCILIATION_REQUIRED", second["transition"]["failures"])
        self.assertEqual(client.create_calls, 1)
        self.assert_shadow()

    def test_readback_repository_or_branch_mismatch_is_unknown(self):
        class WrongReadbackClient(FakeInitialClient):
            def create_session(self, **kwargs):
                self.create_calls += 1
                return {
                    "session": "sessions/wrong",
                    "repository": "other/repo",
                    "starting_branch": "other",
                }

        client = WrongReadbackClient()
        result = execute_initial_lineage_generation(**self.args(client))
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_PROVIDER_READBACK_RECONCILIATION_REQUIRED")
        self.assertEqual(client.create_calls, 1)
        self.assert_shadow()
        lane = self.store.read_workstream(self.lane_id)
        assert lane.record is not None
        self.assertIsInstance(lane.record.unknown_write_state, dict)


if __name__ == "__main__":
    unittest.main()
