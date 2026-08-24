import unittest

from ues.watchdog import (
    evaluate_control_cycle,
    evaluate_lane_watchdog,
    normalize_watchdog_policy,
)


class WatchdogTests(unittest.TestCase):
    def test_waiting_unresolved_and_completed_review_are_incidents(self):
        lane = {
            "lane_id": "W04",
            "waiting_class": "POLICY_RESOLVABLE",
            "waiting_resolved": False,
            "waiting_age_seconds": 5000,
            "review_completed": True,
            "review_routed": False,
            "review_unrouted_age_seconds": 2500,
            "next_action": "CONTINUE_SAME_SESSION",
        }
        result = evaluate_lane_watchdog(lane)
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("WAITING_UNRESOLVED", codes)
        self.assertIn("COMPLETED_REVIEW_NOT_ROUTED", codes)

    def test_adapter_policy_overrides_thresholds(self):
        lane = {
            "lane_id": "W04",
            "waiting_class": "POLICY_RESOLVABLE",
            "waiting_resolved": False,
            "waiting_age_seconds": 31,
            "next_action": "CONTINUE_SAME_SESSION",
        }
        result = evaluate_lane_watchdog(
            lane,
            policy={"thresholds": {"waiting_unresolved_seconds": 30}},
        )
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("WAITING_UNRESOLVED", codes)
        self.assertEqual(result["watchdog_policy_source"], "ADAPTER_OR_CONTROLLER_POLICY")

    def test_configurable_categories_can_exclude_non_applicable_drift(self):
        policy = normalize_watchdog_policy({"enabled_categories": ["FORGOTTEN_LANE"]})
        self.assertEqual(policy["enabled_categories"], {"FORGOTTEN_LANE"})

    def test_unclassified_failure_and_stale_heartbeat_are_detected(self):
        lane = {
            "lane_id": "W05",
            "failed_state": True,
            "failure_classified": False,
            "failure_unclassified_age_seconds": 1000,
            "active": True,
            "role": "WRITER",
            "heartbeat_age_seconds": 2000,
            "next_action": "CLASSIFY_FAILURE",
        }
        result = evaluate_lane_watchdog(lane)
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("FAILED_STATE_UNCLASSIFIED", codes)
        self.assertIn("STALE_ACTIVE_HEARTBEAT", codes)

    def test_forgotten_lane_requires_no_action_and_no_stop_gate(self):
        result = evaluate_lane_watchdog({"lane_id": "forgotten"})
        self.assertTrue(result["forgotten"])

    def test_forgotten_lane_fails_control_cycle(self):
        result = evaluate_control_cycle([{"lane_id": "forgotten"}])
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_FAILED")
        self.assertEqual(result["forgotten_lanes"], ["forgotten"])

    def test_correction_rereview_loop_stall_detected(self):
        result = evaluate_lane_watchdog(
            {
                "lane_id": "W07",
                "rereview_pending": True,
                "correction_rereview_age_seconds": 4000,
                "next_action": "DISPATCH_REVIEW",
            }
        )
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("CORRECTION_REREVIEW_LOOP_STALLED", codes)

    def test_exact_head_evidence_drift_detected(self):
        result = evaluate_lane_watchdog(
            {
                "lane_id": "W08",
                "evidence_drift_unresolved": True,
                "evidence_drift_age_seconds": 2000,
                "next_action": "RECONCILE_EXACT_HEAD",
            }
        )
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED", codes)

    def test_exhausted_reuse_that_drags_critical_path_is_incident(self):
        result = evaluate_lane_watchdog(
            {
                "lane_id": "W09",
                "next_action": "PREPARE_REPLACEMENT",
                "reuse_path_selected": True,
                "reuse_viable": False,
                "replacement_ready": True,
                "reuse_delay_age_seconds": 601,
            },
            policy={"thresholds": {"reuse_critical_path_drag_seconds": 600}},
        )
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("REUSE_CRITICAL_PATH_DRAG", codes)

    def test_blocked_lane_does_not_freeze_other_lane(self):
        result = evaluate_control_cycle(
            [
                {"lane_id": "blocked", "blocked": True, "stop_gate": "PARENT_REQUIRED"},
                {"lane_id": "ready", "blocked": False, "next_action": "ROUTE_REVIEW"},
            ]
        )
        self.assertIn("ready", result["executable_lanes"])
        self.assertFalse(result["blocked_lane_freezes_independent_lanes"])

    def test_untreated_proven_auto_safe_incident_fails_cycle(self):
        result = evaluate_control_cycle(
            [
                {
                    "lane_id": "W04",
                    "next_action": "CONTINUE_SAME_SESSION",
                    "auto_safe_incident_proven": True,
                    "auto_safe_treated": False,
                }
            ]
        )
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_FAILED")
        self.assertEqual(result["unresolved_auto_safe_lanes"], ["W04"])

    def test_parent_required_lane_alone_does_not_false_fail_cycle(self):
        result = evaluate_control_cycle(
            [
                {
                    "lane_id": "parent",
                    "authority": "PARENT_REQUIRED",
                    "blocked": True,
                    "stop_gate": "PARENT_REQUIRED",
                    "waiting_class": "ENVIRONMENT_MISMATCH",
                    "waiting_age_seconds": 9999,
                }
            ]
        )
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_OK")
        self.assertEqual(result["unresolved_auto_safe_lanes"], [])
        self.assertEqual(result["forgotten_lanes"], [])

    def test_missing_active_heartbeat_fails_closed_to_warning(self):
        result = evaluate_lane_watchdog(
            {
                "lane_id": "writer",
                "active": True,
                "role": "WRITER",
                "next_action": "CONTINUE",
            }
        )
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("STALE_ACTIVE_HEARTBEAT", codes)

    def test_terminal_failure_is_counted_without_forgetting_stop_gate(self):
        result = evaluate_control_cycle(
            [
                {
                    "lane_id": "failed",
                    "terminal_failed_session": True,
                    "stop_gate": "PARENT_REQUIRED",
                }
            ]
        )
        self.assertEqual(result["terminal_failed_sessions"], ["failed"])
        self.assertEqual(result["forgotten_lanes"], [])
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_OK")


if __name__ == "__main__":
    unittest.main()
