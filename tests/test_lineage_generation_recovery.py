from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.lineage_generation import persist_created_generation_binding, recover_lineage_policy_from_state
from ues.lineage_registry import match_lineage_session, session_fingerprint
from ues.state_store import DeterministicFileStateStore


class LineageGenerationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_create_confirmed_then_fresh_runner_recovers_generation_without_adapter_fingerprint(self) -> None:
        fp = session_fingerprint("sessions/new-g1")
        persisted = persist_created_generation_binding(
            self.store,
            project="GS",
            route="GS",
            workstream="GS-FINAL-ASSURANCE-PR88-R1",
            role="FINAL_ASSURANCE",
            generation=1,
            session_fingerprint=fp,
            source_name="sources/github/hamad933/GS-2",
            source_repository="hamad933/GS-2",
            provider_starting_branch="remediation/gs-dependency-remediation-r1",
            authority_event_id="GS-G95",
            operation_key="operation-1",
            generation_transition_key="transition-1",
            replacement_cause="FINAL_ASSURANCE_AUTHORIZED",
            candidate_sha="511ae72e49d258a14036548fedb7f6ca6f265352",
            policy_provenance={"authority_current": True},
        )
        self.assertEqual(persisted["status"], "GENERATION_BINDING_PERSISTED")

        # Simulate a new runner using only stable adapter structure.
        recovered = recover_lineage_policy_from_state(
            self.store,
            project="GS",
            route="GS",
            workstream="GS-FINAL-ASSURANCE-PR88-R1",
            role="ASSURANCE",
            stable_policy={
                "provider_starting_branch": "remediation/gs-dependency-remediation-r1",
                "known_session_fingerprints": [],
            },
        )
        self.assertTrue(recovered["_state_generation_recovered"])
        self.assertEqual(recovered["_state_generation"], 1)
        self.assertIn(fp, recovered["known_session_fingerprints"])

        inventory = [
            {
                "_session_fingerprint": fp,
                "_source_repository": "hamad933/GS-2",
                "sourceStartingBranch": "remediation/gs-dependency-remediation-r1",
                "normalizedState": "IN_PROGRESS",
            }
        ]
        binding = match_lineage_session(inventory, recovered, repository="hamad933/GS-2")
        self.assertEqual(binding["status"], "PROVEN")
        self.assertEqual(binding["session_fingerprint"], fp)

    def test_same_generation_same_transition_is_idempotent(self) -> None:
        fp = session_fingerprint("sessions/new-g1")
        kwargs = dict(
            store=self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            generation=1,
            session_fingerprint=fp,
            source_name="sources/github/hamad933/Cybersecurity-Education-Platform",
            source_repository="hamad933/Cybersecurity-Education-Platform",
            provider_starting_branch="work/w02",
            authority_event_id="CEP-CURRENT",
            operation_key="operation-1",
            generation_transition_key="transition-1",
            replacement_cause="IRRECOVERABLY_INVALID_BINDING",
            candidate_sha="a" * 40,
            policy_provenance={},
        )
        first = persist_created_generation_binding(**kwargs)
        second = persist_created_generation_binding(**kwargs)
        self.assertEqual(first["status"], "GENERATION_BINDING_PERSISTED")
        self.assertEqual(second["status"], "IDEMPOTENT_BINDING_PRESENT")


if __name__ == "__main__":
    unittest.main()
