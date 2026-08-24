from __future__ import annotations

import unittest
from datetime import timezone

from ues.bounded_waiting_runtime import _latest_message, _load_adapter, _policy_entries, run
from ues.lifecycle_runtime_current import _waiting_response


class BoundedWaitingRuntimeTests(unittest.TestCase):
    def test_gs_without_policy_is_read_only_noop(self) -> None:
        result = run("GS")
        self.assertEqual(result["result"], "NO_BOUNDED_WAITING_POLICY")
        self.assertFalse(result["provider_mutation_performed"])

    def test_cep_adapter_does_not_embed_current_waiting_responses(self) -> None:
        adapter = _load_adapter("CEP")
        self.assertEqual(_policy_entries(adapter), [])
        self.assertIsNone(_waiting_response(None, "W03", "WRITER"))

        authority = {
            "waiting_responses": {
                "W03:WRITER": {
                    "controller_resolvable": True,
                    "scope_expansion": False,
                    "response": "Continue W03 within the already governed scope.",
                },
                "W04:WRITER": {
                    "controller_resolvable": True,
                    "scope_expansion": False,
                    "response": "Continue W04 within the already governed scope.",
                },
            }
        }
        w03 = _waiting_response(authority, "W03", "WRITER")
        w04 = _waiting_response(authority, "W04", "WRITER")
        self.assertIn("Continue W03", w03 or "")
        self.assertIn("Continue W04", w04 or "")
        self.assertIsNone(_waiting_response(authority, "W02", "WRITER"))

    def test_scope_expanding_waiting_response_is_not_accepted(self) -> None:
        authority = {
            "waiting_responses": {
                "W04:WRITER": {
                    "controller_resolvable": True,
                    "scope_expansion": True,
                    "response": "Expand the work beyond the governed scope.",
                }
            }
        }
        self.assertIsNone(_waiting_response(authority, "W04", "WRITER"))

    def test_latest_message_uses_activity_create_time(self) -> None:
        activities = [
            {
                "name": "sessions/1/activities/a1",
                "createTime": "2026-08-24T00:00:00Z",
                "agentMessaged": {"agentMessage": "first"},
            },
            {
                "name": "sessions/1/activities/a2",
                "createTime": "2026-08-24T00:05:00Z",
                "agentMessaged": {"agentMessage": "second"},
            },
        ]
        latest = _latest_message(activities, "agentMessaged")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest[1], "second")
        self.assertEqual(latest[2].tzinfo, timezone.utc)

    def test_user_message_can_be_compared_against_agent_message_without_persisting_content(self) -> None:
        activities = [
            {
                "name": "sessions/1/activities/a1",
                "createTime": "2026-08-24T00:00:00Z",
                "agentMessaged": {"agentMessage": "need input"},
            },
            {
                "name": "sessions/1/activities/a2",
                "createTime": "2026-08-24T00:01:00Z",
                "userMessaged": {"userMessage": "answer"},
            },
        ]
        agent = _latest_message(activities, "agentMessaged")
        user = _latest_message(activities, "userMessaged")
        self.assertIsNotNone(agent)
        self.assertIsNotNone(user)
        assert agent is not None and user is not None
        self.assertGreater(user[2], agent[2])


if __name__ == "__main__":
    unittest.main()
