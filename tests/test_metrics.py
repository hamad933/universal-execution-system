import unittest

from ues.metrics import build_operational_metrics


class MetricsTests(unittest.TestCase):
    def test_metrics_are_sanitized_and_complete(self):
        lanes = [
            {
                "lane_id": "W04",
                "role": "WRITER",
                "active": True,
                "waiting_age_seconds": 120,
                "time_to_route_review_seconds": 30,
                "time_to_correction_seconds": 45,
                "forgotten": True,
                "auto_safe_incident": True,
                "auto_safe_treated": False,
                "raw_prompt": "SECRET PROMPT VALUE",
                "provider_message": "sensitive text",
            },
            {
                "lane_id": "R04",
                "role": "REVIEWER",
                "active": True,
                "idle": True,
                "terminal_failed_session": True,
                "time_to_route_review_seconds": 60,
            },
        ]
        metrics = build_operational_metrics(
            lanes,
            task_budget={"state": "UNKNOWN_LIFETIME_CONSUMPTION"},
        )
        self.assertEqual(metrics["waiting_age"]["max_seconds"], 120)
        self.assertEqual(metrics["time_to_route_review"]["average_seconds"], 45)
        self.assertEqual(metrics["time_to_correction"]["average_seconds"], 45)
        self.assertEqual(metrics["idle_lane_count"], 1)
        self.assertEqual(metrics["forgotten_lane_count"], 1)
        self.assertEqual(metrics["failed_session_count"], 1)
        self.assertEqual(metrics["active_writer_count"], 1)
        self.assertEqual(metrics["active_reviewer_count"], 1)
        self.assertEqual(metrics["unresolved_auto_safe_count"], 1)
        self.assertEqual(metrics["task_budget_state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertNotIn("SECRET PROMPT VALUE", repr(metrics))
        self.assertNotIn("sensitive text", repr(metrics))
        self.assertTrue(metrics["sanitized"])


if __name__ == "__main__":
    unittest.main()
