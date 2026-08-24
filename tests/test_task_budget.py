import unittest

from ues.task_budget import evaluate_new_task_gate, evaluate_task_budget


class TaskBudgetTests(unittest.TestCase):
    def test_lifetime_uncertainty_fails_closed_by_default(self):
        budget = evaluate_task_budget(
            project="CEP",
            ceiling=20,
            reserve=3,
            lifetime_consumption_known=False,
            proven_lifetime_used=None,
            current_enumerated_tasks=2,
        )
        self.assertEqual(budget["state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])
        self.assertFalse(budget["current_enumeration_proves_lifetime_consumption"])

    def test_gs_style_owner_policy_does_not_freeze_on_unknown_history_alone(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=40,
            reserve=0,
            lifetime_consumption_known=False,
            proven_lifetime_used=5,
            current_enumerated_tasks=5,
            unknown_lifetime_policy="ALLOW_UNLESS_DIRECT_CEILING_REACHED",
            hard_ceiling_reached=False,
        )
        self.assertEqual(
            budget["state"],
            "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME",
        )
        self.assertTrue(budget["budget_allows_new_task"])
        self.assertIsNone(budget["safe_remaining"])
        self.assertEqual(budget["proven_floor_remaining"], 35)
        gate = evaluate_new_task_gate(
            budget,
            parent_gate_satisfied=True,
            automatic_creation_authorized=True,
        )
        self.assertTrue(gate["allowed"])
        self.assertTrue(gate["automatic_creation"])

    def test_direct_hard_ceiling_always_stops_creation(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=40,
            reserve=0,
            lifetime_consumption_known=False,
            proven_lifetime_used=40,
            unknown_lifetime_policy="ALLOW_UNLESS_DIRECT_CEILING_REACHED",
            hard_ceiling_reached=True,
        )
        self.assertEqual(budget["state"], "DIRECT_HARD_CEILING_REACHED")
        self.assertFalse(budget["budget_allows_new_task"])

    def test_proven_usage_respects_reserve(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=10,
            reserve=2,
            lifetime_consumption_known=True,
            proven_lifetime_used=7,
        )
        self.assertEqual(budget["safe_remaining"], 1)
        self.assertTrue(budget["budget_allows_new_task"])

    def test_inconsistent_lifetime_evidence_fails_closed(self):
        budget = evaluate_task_budget(
            project="CEP",
            ceiling=20,
            reserve=3,
            lifetime_consumption_known=True,
            proven_lifetime_used=2,
            current_enumerated_tasks=3,
        )
        self.assertEqual(budget["state"], "TASK_BUDGET_EVIDENCE_INCONSISTENT")
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])

    def test_parent_gate_is_mandatory(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=10,
            reserve=2,
            lifetime_consumption_known=True,
            proven_lifetime_used=1,
        )
        gate = evaluate_new_task_gate(budget, parent_gate_satisfied=False)
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["authority"], "PARENT_ONLY")
        self.assertFalse(gate["automatic_creation"])


if __name__ == "__main__":
    unittest.main()
