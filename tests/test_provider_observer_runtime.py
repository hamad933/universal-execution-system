from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.provider_observer_runtime import collect_resilient_observation, observe


class ActivityFailure(Exception):
    category = "ACTIVITY_READ_FAILED"


class FakeClient:
    def __init__(self, *, fail_waiting_activities: bool = False):
        self.fail_waiting_activities = fail_waiting_activities
        self.activity_calls = []

    def list_sources(self, *, page_size=100):
        return [
            {"name": "sources/gs", "repository": "hamad933/GS-2", "explicitRepositoryIdentity": True},
            {"name": "sources/cep", "repository": "hamad933/Cybersecurity-Education-Platform", "explicitRepositoryIdentity": True},
        ]

    def list_sessions(self, *, page_size=100):
        return [
            {"name": "sessions/gs-failed", "title": "GS-G70-JULES-IPA-HOME-R1", "normalizedState": "FAILED", "stateAuthoritative": True, "sourceIdentifier": "sources/gs"},
            {"name": "sessions/gs-complete", "title": "GS-G70-JULES-IPA-SOLUTIONS", "normalizedState": "COMPLETED", "stateAuthoritative": True, "sourceIdentifier": "sources/gs"},
            {"name": "sessions/cep-waiting", "title": "CEP-W04-R03: Progress & Evidence", "normalizedState": "AWAITING_USER_FEEDBACK", "stateAuthoritative": True, "sourceIdentifier": "sources/cep"},
        ]

    def list_activities(self, session, *, page_size=100):
        self.activity_calls.append(session)
        if self.fail_waiting_activities:
            raise ActivityFailure("private text must not persist")
        return [{
            "name": "activities/private-id",
            "type": "AGENT_MESSAGE",
            "createTime": "2026-08-24T00:00:00Z",
            "agentMessaged": {"agentMessage": "private question"},
        }]


class RuntimeObserverTests(unittest.TestCase):
    def test_completed_sessions_are_read_for_recovery_but_failed_sessions_remain_skipped(self):
        client = FakeClient()
        result = collect_resilient_observation(client, observed_at="2026-08-24T00:10:00Z")
        self.assertEqual(client.activity_calls, ["sessions/gs-complete", "sessions/cep-waiting"])
        gs = result["projects"]["GS"]
        by_state = {item["state"]: item for item in gs["sessions"]}
        self.assertTrue(by_state["FAILED"]["activity_read_skipped"])
        self.assertFalse(by_state["COMPLETED"]["activity_read_skipped"])
        self.assertTrue(by_state["COMPLETED"]["activity_read_complete"])
        self.assertEqual(by_state["COMPLETED"]["_terminal_candidate"]["state"], "COMPLETED_OUTPUT_UNSTRUCTURED")
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["activity_content_persisted"])

    def test_waiting_activity_failure_does_not_erase_provider_inventory(self):
        client = FakeClient(fail_waiting_activities=True)
        with patch("ues.provider_observer_runtime._READ_ERRORS", (ActivityFailure,)):
            result = collect_resilient_observation(client, observed_at="2026-08-24T00:10:00Z")
        cep = result["projects"]["CEP"]
        self.assertEqual(cep["session_count"], 1)
        item = cep["sessions"][0]
        self.assertEqual(item["classification"], "WAITING_INPUT_REQUIRES_RECONCILIATION")
        self.assertFalse(item["activity_read_complete"])
        self.assertEqual(item["activity_read_error_category"], "ACTIVITY_READ_FAILED")
        self.assertNotIn("private text", str(result))

    def test_observe_persists_fail_health_without_exception_text(self):
        health_calls = []

        def fake_health(**kwargs):
            health_calls.append(kwargs)
            return {**kwargs, "version": len(health_calls)}

        class BrokenClient:
            def __init__(self, key):
                pass

            def list_sources(self, *, page_size=100):
                raise ActivityFailure("secret provider details")

        with (
            patch.dict("os.environ", {"JULES_API_KEY": "secret"}, clear=False),
            patch("ues.provider_observer_runtime.persist_health", side_effect=fake_health),
            patch("ues.provider_observer_runtime.JulesClient", BrokenClient),
        ):
            result = observe()
        self.assertEqual(result["result"], "JULES_PROVIDER_OBSERVATION_FAILED")
        self.assertEqual(result["error_category"], "ACTIVITY_READ_FAILED")
        self.assertNotIn("secret provider details", str(result))
        self.assertEqual(health_calls[0]["status"], "IN_FLIGHT")
        self.assertEqual(health_calls[-1]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
