import unittest
from datetime import datetime, timedelta, timezone

from ues.task_budget import (
    evaluate_new_task_gate,
    evaluate_task_budget,
    observe_rolling_quota_window,
)


class TaskBudgetTests(unittest.TestCase):
    def test_quota_window_uncertainty_fails_closed_by_default(self):
        budget = evaluate_task_budget(
            project="CEP",
            ceiling=20,
            reserve=3,
            quota_window_consumption_known=False,
            proven_quota_window_used=None,
            current_window_enumerated_tasks=2,
        )
        self.assertEqual(budget["state_v3"], "UNKNOWN_QUOTA_WINDOW_CONSUMPTION")
        self.assertEqual(budget["state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])
        self.assertEqual(budget["budget_basis"], "CURRENT_QUOTA_WINDOW")
        self.assertFalse(budget["historical_usage_affects_capacity"])

    def test_owner_policy_does_not_freeze_on_unknown_window_alone(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=40,
            reserve=0,
            quota_window_consumption_known=False,
            proven_quota_window_used=None,
            current_window_enumerated_tasks=5,
            unknown_quota_window_policy="ALLOW_UNLESS_DIRECT_CEILING_REACHED",
        )
        self.assertEqual(
            budget["state_v3"],
            "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_QUOTA_WINDOW",
        )
        self.assertEqual(
            budget["state"],
            "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME",
        )
        self.assertTrue(budget["budget_allows_new_task"])
        self.assertIsNone(budget["safe_remaining"])
        self.assertEqual(budget["observed_headroom"], 35)
        gate = evaluate_new_task_gate(
            budget,
            parent_gate_satisfied=True,
            automatic_creation_authorized=True,
        )
        self.assertTrue(gate["allowed"])
        self.assertTrue(gate["automatic_creation"])

    def test_current_window_at_hard_ceiling_stops_creation(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=40,
            reserve=0,
            quota_window_consumption_known=False,
            current_window_enumerated_tasks=40,
            unknown_quota_window_policy="ALLOW_UNLESS_DIRECT_CEILING_REACHED",
        )
        self.assertEqual(budget["state"], "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED")
        self.assertTrue(budget["hard_ceiling_reached"])
        self.assertFalse(budget["budget_allows_new_task"])

    def test_missing_runtime_ceiling_fails_closed_without_fabricating_hard_limit(self):
        budget = evaluate_task_budget(
            project="RP03",
            ceiling=None,
            reserve=0,
            quota_window_consumption_known=True,
            proven_quota_window_used=8,
            current_window_enumerated_tasks=8,
            hard_ceiling_reached=False,
        )
        self.assertEqual(budget["state"], "CAPACITY_CEILING_UNRESOLVED")
        self.assertEqual(budget["state_v3"], "CAPACITY_CEILING_UNRESOLVED")
        self.assertIsNone(budget["ceiling"])
        self.assertFalse(budget["ceiling_resolved"])
        self.assertFalse(budget["hard_ceiling_reached"])
        self.assertIsNone(budget["safe_remaining"])
        self.assertIsNone(budget["observed_headroom"])
        self.assertFalse(budget["budget_allows_new_task"])
        self.assertTrue(budget["fail_closed"])

    def test_direct_provider_limit_still_wins_when_numeric_ceiling_is_unresolved(self):
        budget = evaluate_task_budget(
            project="RP03",
            ceiling=None,
            reserve=0,
            quota_window_consumption_known=True,
            proven_quota_window_used=8,
            current_window_enumerated_tasks=8,
            hard_ceiling_reached=True,
        )
        self.assertEqual(budget["state"], "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED")
        self.assertTrue(budget["hard_ceiling_reached"])
        self.assertFalse(budget["budget_allows_new_task"])

    def test_proven_window_usage_respects_reserve(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=10,
            reserve=2,
            quota_window_consumption_known=True,
            proven_quota_window_used=7,
            current_window_enumerated_tasks=7,
        )
        self.assertEqual(budget["safe_remaining"], 1)
        self.assertTrue(budget["budget_allows_new_task"])

    def test_inconsistent_window_evidence_fails_closed(self):
        budget = evaluate_task_budget(
            project="CEP",
            ceiling=20,
            reserve=3,
            quota_window_consumption_known=True,
            proven_quota_window_used=2,
            current_window_enumerated_tasks=3,
        )
        self.assertEqual(budget["state"], "TASK_BUDGET_EVIDENCE_INCONSISTENT")
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])

    def test_parent_gate_is_mandatory(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=10,
            reserve=2,
            quota_window_consumption_known=True,
            proven_quota_window_used=1,
            current_window_enumerated_tasks=1,
        )
        gate = evaluate_new_task_gate(budget, parent_gate_satisfied=False)
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["authority"], "PARENT_ONLY")
        self.assertFalse(gate["automatic_creation"])

    def test_rolling_window_excludes_prior_history_from_capacity(self):
        now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        tasks = [
            {"createTime": (now - timedelta(hours=30)).isoformat().replace("+00:00", "Z")},
            {"createTime": (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")},
            {"createTime": (now - timedelta(hours=23)).isoformat().replace("+00:00", "Z")},
            {"createTime": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        ]
        observed = observe_rolling_quota_window(tasks, now=now, window_seconds=24 * 60 * 60)
        self.assertTrue(observed["quota_window_consumption_known"])
        self.assertEqual(observed["current_window_enumerated_tasks"], 2)
        self.assertEqual(observed["proven_quota_window_used"], 2)
        self.assertEqual(observed["historical_outside_window_tasks"], 2)
        self.assertEqual(observed["provider_inventory_total"], 4)
        self.assertFalse(observed["historical_usage_affects_capacity"])

        budget = evaluate_task_budget(
            project="GS",
            ceiling=100,
            reserve=0,
            **{
                key: observed[key]
                for key in (
                    "quota_window_consumption_known",
                    "proven_quota_window_used",
                    "current_window_enumerated_tasks",
                )
            },
        )
        self.assertEqual(budget["safe_remaining"], 98)
        self.assertEqual(budget["observed_used_lower_bound"], 2)
        self.assertEqual(budget["current_enumerated_tasks"], 2)

    def test_missing_provider_timestamp_makes_complete_window_unknown(self):
        now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        observed = observe_rolling_quota_window(
            [
                {"createTime": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
                {"name": "sessions/unknown-time"},
            ],
            now=now,
        )
        self.assertFalse(observed["quota_window_consumption_known"])
        self.assertIsNone(observed["proven_quota_window_used"])
        self.assertEqual(observed["current_window_enumerated_tasks"], 1)
        self.assertEqual(observed["unknown_timestamp_tasks"], 1)

    def test_legacy_argument_names_are_compatibility_only(self):
        budget = evaluate_task_budget(
            project="GS",
            ceiling=10,
            reserve=0,
            lifetime_consumption_known=True,
            proven_lifetime_used=2,
            current_enumerated_tasks=2,
        )
        self.assertEqual(budget["budget_basis"], "CURRENT_QUOTA_WINDOW")
        self.assertFalse(budget["historical_usage_affects_capacity"])
        self.assertEqual(budget["safe_remaining"], 8)


if __name__ == "__main__":
    unittest.main()
