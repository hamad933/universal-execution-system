from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.binding_safe_generation import execute_binding_safe_generation
from ues.generation_reconciliation import reconcile_unknown_generation
from ues.lineage_generation import recover_lineage_policy_from_state
from ues.lineage_registry import match_lineage_session, upsert_lineage_observation
from ues.providers.base import WriteOutcomeUnknown
from ues.state_store import DeterministicFileStateStore
from ues.task_budget_accounting import read_budget_accounting, record_confirmed_generation


class UnknownCreateClient:
    def __init__(self) -> None:
        self.create_calls = 0

    def create_session(self, **kwargs):
        self.create_calls += 1
        raise WriteOutcomeUnknown(
            "simulated lost create response",
            operation="jules.sessions.create",
            recovery={"verdict": "AUTHORITATIVE_SESSION_ENUMERATION_REQUIRED", "safe_to_blind_retry": False},
        )


class GenerationReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        upsert_lineage_observation(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            binding={"status": "UNBOUND", "reason": "NO_MATCH", "provider_state": "UNKNOWN"},
            policy={"known_session_fingerprints": []},
            current_candidate_sha="a" * 40,
            current_pr_number=31,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def policy() -> dict:
        return {
            "necessary_generation_authorized": True,
            "generation_effect_authorized": True,
            "generation_budget_safe": True,
            "budget": {"hard_ceiling_reached": False},
            "provenance": {"authority_current": True},
        }

    def test_ambiguous_create_is_reconciled_from_marker_without_second_create(self) -> None:
        client = UnknownCreateClient()
        first = execute_binding_safe_generation(
            self.store,
            client,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            prompt="continue W02",
            title="CEP W02 WRITER",
            source_name="sources/github/hamad933/Cybersecurity-Education-Platform",
            starting_branch="work/cep-w02-parent-reconciliation-r01",
            repository="hamad933/Cybersecurity-Education-Platform",
            authority_event_id="CEP-CURRENT",
            current_policy=self.policy(),
            replacement_cause="IRRECOVERABLY_INVALID_BINDING",
            candidate_sha="a" * 40,
            work_remaining=True,
            active_duplicate_absent=True,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
        )
        self.assertEqual(first["decision"], "CREATE_SESSION_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED")
        transition = first["transition"]
        marker = str(transition["transition_key"])[:12]
        inventory = [
            {
                "name": "sessions/reconciled-g1",
                "title": f"CEP W02 WRITER [{marker}]",
                "_source_repository": "hamad933/Cybersecurity-Education-Platform",
                "sourceStartingBranch": "work/cep-w02-parent-reconciliation-r01",
                "normalizedState": "IN_PROGRESS",
            }
        ]
        reconciled = reconcile_unknown_generation(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            inventory=inventory,
            authority_event_id="CEP-CURRENT",
            policy_provenance={"authority_current": True},
        )
        self.assertEqual(reconciled["decision"], "AMBIGUOUS_GENERATION_AUTHORITATIVELY_RECONCILED")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(read_budget_accounting(self.store, project="CEP", route="PERSONAL:CEP")["ues_confirmed_generation_count"], 1)

        recovered = recover_lineage_policy_from_state(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            stable_policy={"known_session_fingerprints": []},
        )
        binding = match_lineage_session(inventory, recovered, repository="hamad933/Cybersecurity-Education-Platform")
        self.assertEqual(binding["status"], "PROVEN")

    def test_zero_provider_match_stays_unknown_and_does_not_retry(self) -> None:
        client = UnknownCreateClient()
        first = execute_binding_safe_generation(
            self.store,
            client,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            prompt="continue W02",
            title="CEP W02 WRITER",
            source_name="sources/github/hamad933/Cybersecurity-Education-Platform",
            starting_branch="work/cep-w02-parent-reconciliation-r01",
            repository="hamad933/Cybersecurity-Education-Platform",
            authority_event_id="CEP-CURRENT",
            current_policy=self.policy(),
            replacement_cause="IRRECOVERABLY_INVALID_BINDING",
            candidate_sha="a" * 40,
            work_remaining=True,
            active_duplicate_absent=True,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
        )
        self.assertIn("UNKNOWN", first["decision"])
        reconciled = reconcile_unknown_generation(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            inventory=[],
            authority_event_id="CEP-CURRENT",
        )
        self.assertEqual(reconciled["decision"], "GENERATION_UNKNOWN_NOT_YET_OBSERVED")
        self.assertFalse(reconciled["safe_to_blind_retry"])
        self.assertEqual(client.create_calls, 1)

    def test_accounting_is_once_only(self) -> None:
        first = record_confirmed_generation(
            self.store,
            project="GS",
            route="GS",
            operation_key="op-1",
            generation_transition_key="transition-1",
        )
        second = record_confirmed_generation(
            self.store,
            project="GS",
            route="GS",
            operation_key="op-1",
            generation_transition_key="transition-1",
        )
        self.assertEqual(first["ues_confirmed_generation_count"], 1)
        self.assertEqual(second["status"], "IDEMPOTENT_GENERATION_ALREADY_ACCOUNTED")
        self.assertEqual(second["ues_confirmed_generation_count"], 1)


if __name__ == "__main__":
    unittest.main()
