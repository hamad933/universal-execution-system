import unittest

from ues.watchdog import evaluate_control_cycle, evaluate_lane_watchdog


class WatchdogTests(unittest.TestCase):
    def test_w04_waiting_and_completed_review_are_incidents(self):
        lane = {
            "lane_id": "W04",
            "waiting_class": "POLICY_RESOLVABLE",
            "waiting_age_seconds": 5000,
            "review_completed": True,
            "review_routed": False,
            "review_unrouted_age_seconds": 2500,
            "next_action": "CONTINUE_SAME_SESSION",
        }
        result = evaluate_lane_watchdog(lane)
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("WAITING_TOO_LONG", codes)
        self.assertIn("COMPLETED_REVIEW_NOT_ROUTED", codes)

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

    def test_blocked_lane_does_not_freeze_other_lane(self):
        result = evaluate_control_cycle([
            {"lane_id": "blocked", "blocked": True, "stop_gate": "PARENT_REQUIRED"},
            {"lane_id": "ready", "blocked": False, "next_action": "ROUTE_REVIEW"},
        ])
        self.assertIn("ready", result["executable_lanes"])
        self.assertFalse(result["blocked_lane_freezes_independent_lanes"])

    def test_untreated_auto_safe_incident_fails_cycle(self):
        result = evaluate_control_cycle([
            {
                "lane_id": "W04",
                "next_action": "CONTINUE_SAME_SESSION",
                "auto_safe_incident": True,
                "auto_safe_treated": False,
            }
        ])
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_FAILED")
        self.assertEqual(result["unresolved_auto_safe_lanes"], ["W04"])

    def test_missing_active_heartbeat_fails_closed_to_warning(self):
        result = evaluate_lane_watchdog({
            "lane_id": "writer",
            "active": True,
            "role": "WRITER",
            "next_action": "CONTINUE",
        })
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("STALE_ACTIVE_HEARTBEAT", codes)

    def test_completed_review_without_age_is_still_incident(self):
        result = evaluate_lane_watchdog({
            "lane_id": "review",
            "review_completed": True,
            "review_routed": False,
            "next_action": "ROUTE_REVIEW",
        })
        codes = {item["code"] for item in result["incidents"]}
        self.assertIn("COMPLETED_REVIEW_NOT_ROUTED", codes)

    def test_terminal_failure_is_counted_without_forgetting_stop_gate(self):
        result = evaluate_control_cycle([
            {
                "lane_id": "failed",
                "terminal_failed_session": True,
                "stop_gate": "PARENT_REQUIRED",
            }
        ])
        self.assertEqual(result["terminal_failed_sessions"], ["failed"])
        self.assertEqual(result["forgotten_lanes"], [])


if __name__ == "__main__":
    unittest.main()
