from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ues.initial_lineage_effects import execute_initial_lineage_generation
from ues.initial_lineage_reconciliation import reconcile_unknown_initial_lineage
from ues.lineage_registry import lineage_lane_id
from ues.state_store import DeterministicFileStateStore, StateUnavailable
from ues.task_budget_accounting import read_budget_accounting


class AmbiguousCreateClient:
    def __init__(self) -> None:
        self.create_calls = 0

    def create_session(self, **kwargs):
        self.create_calls += 1
        raise RuntimeError("lost provider response after possible create")


class InitialLineageReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.adapter = {
            "project": "RP01",
            "route": "RP01",
            "repository": "hamad933/Bayt-Style",
            "authority_transport": {"controller_actor_allowlist": ["hamad933"]},
        }
        self.task_spec = {
            "objective": "Implement only governed W11",
            "exact_baseline": "main@" + "a" * 40,
            "write_scope": ["src/**"],
            "prohibited_scope": ["deploy/**"],
            "validation": ["unit"],
            "evidence": ["exact-head-ci"],
            "handoff": "Return exact evidence and Draft PR",
            "stop_gate": "DRAFT_PR_AND_EXACT_HEAD_CI",
        }
        self.authority = {
            "source": "DRIVE_CURRENT_STATE",
            "source_id": "drive:rp01-current",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH-UNKNOWN-001",
            "expires_at": "2026-08-24T23:00:00Z",
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
            "provenance": {"source": "DRIVE_CURRENT_STATE", "authority_event_id": "RP01-AUTH-UNKNOWN-001"},
        }
        self.lane_id = lineage_lane_id("RP01", "RP01", "W11", "WRITER")
        self.source_name = "sources/github/hamad933/Bayt-Style"
        self.starting_branch = "main"
        self.repository = "hamad933/Bayt-Style"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_unknown(self) -> tuple[AmbiguousCreateClient, dict]:
        client = AmbiguousCreateClient()
        result = execute_initial_lineage_generation(
            self.store,
            client,
            adapter=self.adapter,
            authority=self.authority,
            transport_actor="hamad933",
            authority_now=datetime(2026, 8, 24, 18, 30, tzinfo=timezone.utc),
            current_policy=self.policy,
            project="RP01",
            route="RP01",
            workstream="W11",
            role="WRITER",
            task_spec=self.task_spec,
            prompt="Implement W11 under the exact governed task contract.",
            title="RP01 W11 Writer G1",
            source_name=self.source_name,
            starting_branch=self.starting_branch,
            repository=self.repository,
            candidate_sha="a" * 40,
            active_duplicate_absent=True,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
        )
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_UNEXPECTED_PROVIDER_ERROR_RECONCILIATION_REQUIRED")
        self.assertEqual(client.create_calls, 1)
        read = self.store.read_workstream(self.lane_id)
        self.assertEqual(read.status, "OK")
        assert read.record is not None
        self.assertIsInstance(read.record.unknown_write_state, dict)
        pending = (read.record.evidence_bindings or {}).get("pending_initial_lineage_transition")
        self.assertIsInstance(pending, dict)
        self.assertEqual(read.record.activation_mode, "SHADOW")
        return client, dict(pending)

    def _session(self, pending: dict, *, name: str = "sessions/recovered-1", **overrides) -> dict:
        session = {
            "name": name,
            "title": f"RP01 W11 Writer G1 [{pending['provider_title_marker']}]",
            "_source_repository": self.repository,
            "_source_name": self.source_name,
            "sourceStartingBranch": self.starting_branch,
            "normalizedState": "QUEUED",
        }
        session.update(overrides)
        return session

    def _reconcile(self, inventory):
        return reconcile_unknown_initial_lineage(
            self.store,
            project="RP01",
            route="RP01",
            workstream="W11",
            role="WRITER",
            inventory=inventory,
        )

    def test_zero_matches_stays_unknown_and_never_creates_again(self):
        client, pending = self._make_unknown()
        result = self._reconcile([])
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_UNKNOWN_NOT_YET_OBSERVED")
        self.assertEqual(result["match_count"], 0)
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(client.create_calls, 1)
        read = self.store.read_workstream(self.lane_id)
        assert read.record is not None
        self.assertIsInstance(read.record.unknown_write_state, dict)
        self.assertEqual((read.record.evidence_bindings or {})["pending_initial_lineage_transition"]["transition_key"], pending["transition_key"])
        self.assertEqual(read_budget_accounting(self.store, project="RP01", route="RP01")["ues_confirmed_generation_count"], 0)

    def test_multiple_exact_matches_remain_ambiguous_and_unknown(self):
        client, pending = self._make_unknown()
        inventory = [
            self._session(pending, name="sessions/recovered-1"),
            self._session(pending, name="sessions/recovered-2"),
        ]
        result = self._reconcile(inventory)
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_RECONCILIATION_AMBIGUOUS_DUPLICATE")
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(client.create_calls, 1)
        read = self.store.read_workstream(self.lane_id)
        assert read.record is not None
        self.assertIsInstance(read.record.unknown_write_state, dict)
        self.assertEqual(read_budget_accounting(self.store, project="RP01", route="RP01")["ues_confirmed_generation_count"], 0)

    def test_repository_source_branch_and_marker_must_all_match(self):
        _, pending = self._make_unknown()
        inventory = [
            self._session(pending, name="sessions/wrong-repo", _source_repository="other/repo"),
            self._session(pending, name="sessions/wrong-source", _source_name="sources/github/other/repo"),
            self._session(pending, name="sessions/wrong-branch", sourceStartingBranch="other"),
            self._session(pending, name="sessions/wrong-marker", title="RP01 W11 Writer G1 [differentmark]"),
        ]
        result = self._reconcile(inventory)
        self.assertEqual(result["decision"], "INITIAL_LINEAGE_UNKNOWN_NOT_YET_OBSERVED")
        self.assertEqual(result["match_count"], 0)

    def test_exact_one_match_is_adopted_and_accounted_without_provider_write(self):
        client, pending = self._make_unknown()
        result = self._reconcile([self._session(pending)])
        self.assertEqual(result["decision"], "UNKNOWN_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED")
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["generation"], 1)
        self.assertFalse(result["provider_write_attempted"])
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(client.create_calls, 1)

        read = self.store.read_workstream(self.lane_id)
        assert read.record is not None
        evidence = read.record.evidence_bindings or {}
        self.assertEqual(evidence["generation"], 1)
        self.assertEqual(evidence["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
        self.assertEqual(evidence["initial_lineage_transition_key"], pending["transition_key"])
        self.assertNotIn("pending_initial_lineage_transition", evidence)
        self.assertIsNone(read.record.unknown_write_state)
        self.assertEqual(read.record.activation_mode, "SHADOW")
        self.assertFalse(evidence["raw_session_id_persisted"])
        budget = read_budget_accounting(self.store, project="RP01", route="RP01")
        self.assertEqual(budget["ues_confirmed_generation_count"], 1)

    def test_reconciliation_requires_durable_unknown_state(self):
        with self.assertRaises(StateUnavailable):
            self._reconcile([])


if __name__ == "__main__":
    unittest.main()
