from __future__ import annotations

import unittest

from ues.generation_transition import assess_generation_transition


class GenerationTransitionTests(unittest.TestCase):
    def policy(self, allowed: bool = True) -> dict:
        return {
            "necessary_generation_authorized": allowed,
            "generation_effect_authorized": allowed,
            "generation_budget_safe": allowed,
            "budget": {"hard_ceiling_reached": False},
        }

    def assess(self, **overrides):
        args = dict(
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            role="WRITER",
            current_generation=1,
            predecessor_session_fingerprint="a" * 64,
            candidate_sha="b" * 40,
            replacement_cause="IRRECOVERABLY_INVALID_BINDING",
            work_remaining=True,
            current_policy=self.policy(),
            active_duplicate_absent=True,
            unknown_write_state=False,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
            replacement_task_spec_ready=True,
        )
        args.update(overrides)
        return assess_generation_transition(**args)

    def test_cep_w02_irrecoverably_unbound_replacement_allowed(self) -> None:
        result = self.assess()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["next_generation"], 2)
        self.assertEqual(result["minimum_generation_count"], 1)

    def test_cep_auto_repeated_acknowledged_noop_replacement_allowed(self) -> None:
        result = self.assess(
            workstream="CEP-AUTO-001",
            replacement_cause="REPEATED_PROVEN_INEFFECTIVENESS",
        )
        self.assertTrue(result["allowed"])

    def test_gs_final_assurance_first_physical_generation_allowed(self) -> None:
        result = self.assess(
            project="GS",
            route="GS",
            workstream="GS-FINAL-ASSURANCE-PR88-R1",
            role="FINAL_ASSURANCE",
            current_generation=0,
            predecessor_session_fingerprint=None,
            replacement_cause="FINAL_ASSURANCE_AUTHORIZED",
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["next_generation"], 1)

    def test_active_duplicate_blocks(self) -> None:
        result = self.assess(active_duplicate_absent=False)
        self.assertFalse(result["allowed"])
        self.assertIn("ACTIVE_DUPLICATE_CHECK_REQUIRED", result["failures"])

    def test_unknown_write_blocks_before_retry(self) -> None:
        result = self.assess(unknown_write_state=True)
        self.assertFalse(result["allowed"])
        self.assertIn("UNKNOWN_WRITE_RECONCILIATION_REQUIRED", result["failures"])

    def test_candidate_sha_changes_transition_key(self) -> None:
        first = self.assess(candidate_sha="b" * 40)
        second = self.assess(candidate_sha="c" * 40)
        self.assertNotEqual(first["transition_key"], second["transition_key"])

    def test_terminal_state_alone_is_not_replacement_cause(self) -> None:
        result = self.assess(replacement_cause="FAILED")
        self.assertFalse(result["allowed"])
        self.assertIn("REPLACEMENT_CAUSE_NOT_GOVERNED", result["failures"])


if __name__ == "__main__":
    unittest.main()
