from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.bounded_waiting_supervisor import _summary, run_supervised


class BoundedWaitingSupervisorTests(unittest.TestCase):
    def test_summary_persists_decisions_not_provider_content(self) -> None:
        result = {
            "project": "CEP",
            "result": "BOUNDED_WAITING_RUNTIME_COMPLETE",
            "results": [
                {"workstream": "W03", "decision": "WAITING_ALREADY_HAS_NEWER_OR_EQUAL_USER_RESPONSE", "private": "do-not-persist"},
                {"workstream": "W04", "decision": "BOUNDED_WAITING_CONTINUATION_CONFIRMED", "private": "do-not-persist"},
            ],
            "external_effects_dispatched": 1,
            "new_tasks_or_sessions_created": 0,
        }
        summary = _summary(result)
        self.assertEqual(summary["project"], "CEP")
        self.assertEqual(summary["external_effects_dispatched"], 1)
        self.assertEqual(summary["decision_counts"]["BOUNDED_WAITING_CONTINUATION_CONFIRMED"], 1)
        self.assertNotIn("do-not-persist", str(summary))
        self.assertFalse(summary["raw_session_ids_persisted"])
        self.assertFalse(summary["activity_content_persisted"])

    def test_supervisor_records_complete_even_when_runtime_is_safe_noop(self) -> None:
        runtime_result = {
            "project": "CEP",
            "result": "BOUNDED_WAITING_RUNTIME_COMPLETE",
            "results": [
                {"workstream": "W03", "decision": "WAITING_ALREADY_HAS_NEWER_OR_EQUAL_USER_RESPONSE"},
                {"workstream": "W04", "decision": "NO_MATCHING_AWAITING_SESSION"},
            ],
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
        }
        health_calls = []

        def fake_health(**kwargs):
            health_calls.append(kwargs)
            return {"lane_id": "health", "version": len(health_calls), **kwargs}

        with (
            patch("ues.bounded_waiting_supervisor.persist_cycle", side_effect=fake_health),
            patch("ues.bounded_waiting_supervisor.run_bounded_waiting", return_value=runtime_result),
        ):
            result = run_supervised("CEP")

        self.assertEqual(result["result"], "BOUNDED_EXISTING_SESSION_SUPERVISED_COMPLETE")
        self.assertEqual(health_calls[0]["status"], "IN_FLIGHT")
        self.assertEqual(health_calls[-1]["status"], "PASS")
        self.assertEqual(result["cycle_summary"]["external_effects_dispatched"], 0)

    def test_supervisor_records_failure_without_exception_text(self) -> None:
        health_calls = []

        def fake_health(**kwargs):
            health_calls.append(kwargs)
            return {"lane_id": "health", "version": len(health_calls), **kwargs}

        with (
            patch("ues.bounded_waiting_supervisor.persist_cycle", side_effect=fake_health),
            patch("ues.bounded_waiting_supervisor.run_bounded_waiting", side_effect=RuntimeError("private provider detail")),
        ):
            result = run_supervised("CEP")

        self.assertEqual(result["result"], "BOUNDED_EXISTING_SESSION_SUPERVISED_FAILED")
        self.assertEqual(result["error_category"], "RUNTIMEERROR")
        self.assertNotIn("private provider detail", str(result))
        self.assertEqual(health_calls[-1]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
