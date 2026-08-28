from __future__ import annotations

import unittest

from ues.generation_transition import assess_generation_transition, assess_initial_lineage_creation


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

    def assess_initial(self, **overrides):
        args = dict(
            project="RP01",
            route="RP01",
            workstream="IPA-S01",
            role="REVIEWER",
            current_generation=0,
            predecessor_session_fingerprint=None,
            candidate_sha="b" * 40,
            current_policy=self.policy(),
            active_duplicate_absent=True,
            unknown_write_state=False,
            effect_in_flight=False,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
            initial_task_spec={
                "objective": "Review the governed frozen S01 candidate",
                "write_scope": [],
                "stop_gate": "READ_ONLY_REVIEW_RETURNED",
            },
        )
        args.update(overrides)
        return assess_initial_lineage_creation(**args)

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

    def test_initial_logical_lineage_is_explicit_generation_one(self) -> None:
        result = self.assess_initial()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
        self.assertEqual(result["current_generation"], 0)
        self.assertEqual(result["next_generation"], 1)
        self.assertTrue(result["initial_logical_lineage"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_initial_lineage_cannot_replace_existing_generation(self) -> None:
        result = self.assess_initial(current_generation=1, predecessor_session_fingerprint="a" * 64)
        self.assertFalse(result["allowed"])
        self.assertIn("INITIAL_LINEAGE_ALREADY_EXISTS", result["failures"])

    def test_initial_lineage_requires_structured_task_and_exact_bindings(self) -> None:
        result = self.assess_initial(
            initial_task_spec=None,
            exact_repository_binding=False,
            exact_starting_ref_binding=False,
        )
        self.assertFalse(result["allowed"])
        self.assertIn("INITIAL_TASK_SPEC_REQUIRED", result["failures"])
        self.assertIn("EXACT_REPOSITORY_BINDING_REQUIRED", result["failures"])
        self.assertIn("EXACT_STARTING_REF_BINDING_REQUIRED", result["failures"])

    def test_initial_lineage_unknown_or_inflight_effect_blocks(self) -> None:
        unknown = self.assess_initial(unknown_write_state=True)
        inflight = self.assess_initial(effect_in_flight=True)
        self.assertIn("UNKNOWN_WRITE_RECONCILIATION_REQUIRED", unknown["failures"])
        self.assertIn("EFFECT_IN_FLIGHT_RECONCILIATION_REQUIRED", inflight["failures"])

    def test_initial_task_spec_changes_transition_key(self) -> None:
        first = self.assess_initial(initial_task_spec={"objective": "first"})
        second = self.assess_initial(initial_task_spec={"objective": "second"})
        self.assertNotEqual(first["transition_key"], second["transition_key"])

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

    def test_stale_review_current_sha_rereview_is_remaining_review_work(self) -> None:
        result = self.assess(
            project="RP02",
            route="RP02",
            workstream="RP02-IPA-S01-001",
            role="REVIEWER",
            replacement_cause="STALE_REVIEW_REQUIRES_CURRENT_SHA_REREVIEW",
            work_remaining=False,
        )
        self.assertTrue(result["allowed"])
        self.assertNotIn("NO_REMAINING_WORK", result["failures"])
        self.assertEqual(result["next_generation"], 2)

    def test_non_stale_cause_still_requires_remaining_work(self) -> None:
        result = self.assess(
            replacement_cause="IRRECOVERABLY_INVALID_BINDING",
            work_remaining=False,
        )
        self.assertFalse(result["allowed"])
        self.assertIn("NO_REMAINING_WORK", result["failures"])

    def test_noncanonical_correction_rereview_alias_remains_rejected(self) -> None:
        result = self.assess(
            role="REVIEWER",
            replacement_cause="CORRECTION_REREVIEW_REQUIRED",
            work_remaining=True,
        )
        self.assertFalse(result["allowed"])
        self.assertIn("REPLACEMENT_CAUSE_NOT_GOVERNED", result["failures"])

    def test_terminal_state_alone_is_not_replacement_cause(self) -> None:
        result = self.assess(replacement_cause="FAILED")
        self.assertFalse(result["allowed"])
        self.assertIn("REPLACEMENT_CAUSE_NOT_GOVERNED", result["failures"])


if __name__ == "__main__":
    unittest.main()
