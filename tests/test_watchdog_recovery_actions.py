from __future__ import annotations

import unittest

from ues.watchdog import evaluate_control_cycle, evaluate_lane_watchdog


class WatchdogRecoveryActionTests(unittest.TestCase):
    def test_forgotten_and_drift_cases_emit_exact_actions_not_wait(self) -> None:
        result = evaluate_lane_watchdog(
            {
                "lane_id": "W05",
                "next_action": "TRIGGER_EVIDENCE",
                "adapter_authority_drift": True,
                "route_profile_not_exercised": True,
                "triggerable_ci_not_triggered": True,
            }
        )
        actions = set(result["recommended_actions"])
        self.assertIn("RESOLVE_CURRENT_GOVERNED_AUTHORITY_IGNORE_STALE_SNAPSHOT", actions)
        self.assertIn("BOUNDED_WORKFLOW_DISPATCH_EXACT_PROFILE", actions)
        self.assertIn("TRIGGER_EXACT_HEAD_CI_OR_EVIDENCE", actions)
        self.assertTrue(all(item["recommend_wait_only"] is False for item in result["incidents"]))

    def test_completed_writer_and_findings_route_immediately(self) -> None:
        result = evaluate_lane_watchdog(
            {
                "lane_id": "W02",
                "next_action": "ROUTE",
                "completed_output_unconsumed": True,
                "review_findings_unrouted": True,
                "corrected_sha_not_rereviewed": True,
            }
        )
        actions = set(result["recommended_actions"])
        self.assertIn("VERIFY_CANDIDATE_SHA_AND_ROUTE_COMPLETED_OUTPUT_NOW", actions)
        self.assertIn("ROUTE_EXACT_SHA_FINDINGS_TO_SAME_WRITER_LINEAGE", actions)
        self.assertIn("INVALIDATE_STALE_REVIEW_AND_REREVIEW_CORRECTED_SHA", actions)

    def test_blocked_duplicate_lane_does_not_freeze_independent_triggerable_lane(self) -> None:
        cycle = evaluate_control_cycle(
            [
                {
                    "lane_id": "blocked-duplicate",
                    "blocked": True,
                    "stop_gate": "DUPLICATE_RECONCILIATION_REQUIRED",
                    "duplicate_active_lineage": True,
                },
                {
                    "lane_id": "independent-ci",
                    "next_action": "TRIGGER_EXACT_HEAD_CI_OR_EVIDENCE",
                    "triggerable_ci_not_triggered": True,
                },
            ]
        )
        self.assertIn("independent-ci", cycle["executable_lanes"])
        self.assertFalse(cycle["blocked_lane_freezes_independent_lanes"])


if __name__ == "__main__":
    unittest.main()
