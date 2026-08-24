from __future__ import annotations

import unittest
from datetime import timezone

from ues.bounded_waiting_runtime import _latest_message, _load_adapter, _policy_entries, run


class BoundedWaitingRuntimeTests(unittest.TestCase):
    def test_gs_without_policy_is_read_only_noop(self) -> None:
        result = run("GS")
        self.assertEqual(result["result"], "NO_BOUNDED_WAITING_POLICY")
        self.assertFalse(result["provider_mutation_performed"])

    def test_cep_policy_is_bounded_to_w03_w04_existing_branches(self) -> None:
        adapter = _load_adapter("CEP")
        entries = _policy_entries(adapter)
        self.assertEqual([item["workstream"] for item in entries], ["W03", "W04"])
        self.assertEqual(
            entries[0]["starting_branch"],
            "work/cep-w03-parent-reconciliation-r01-13880329436073387601",
        )
        self.assertEqual(entries[1]["starting_branch"], "work/cep-w04-parent-reconciliation-r02")
        self.assertTrue(all(item["response"] for item in entries))

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
