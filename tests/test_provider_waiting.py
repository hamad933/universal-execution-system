from __future__ import annotations

import json
import unittest

from ues.provider_observer import ProjectTarget
from ues.provider_waiting import reconcile_waiting_sessions


class FakeClient:
    def __init__(self):
        self.sources = [
            {
                "name": "sources/cep",
                "explicitRepositoryIdentity": True,
                "repository": "hamad933/Cybersecurity-Education-Platform",
            },
            {
                "name": "sources/gs",
                "explicitRepositoryIdentity": True,
                "repository": "hamad933/GS-2",
            },
        ]
        self.sessions = [
            {
                "name": "sessions/w04-private-id",
                "normalizedState": "AWAITING_USER_FEEDBACK",
                "sourceIdentifier": "sources/cep",
                "sourceStartingBranch": "work/cep-w04-parent-reconciliation-r02",
            },
            {
                "name": "sessions/gs-terminal-private-id",
                "normalizedState": "FAILED",
                "sourceIdentifier": "sources/gs",
                "sourceStartingBranch": "review/gs-home",
            },
        ]
        self.activities = {
            "sessions/w04-private-id": [
                {
                    "name": "sessions/w04-private-id/activities/q1",
                    "agentMessaged": {"agentMessage": "old private question"},
                },
                {
                    "name": "sessions/w04-private-id/activities/a1",
                    "userMessaged": {"userMessage": "old private answer"},
                },
                {
                    "name": "sessions/w04-private-id/activities/q2",
                    "agentMessaged": {"agentMessage": "new private question"},
                },
            ]
        }
        self.activity_reads = []

    def list_sources(self, *, page_size=100):
        self.source_page_size = page_size
        return list(self.sources)

    def list_sessions(self, *, page_size=100):
        self.session_page_size = page_size
        return list(self.sessions)

    def list_activities(self, session, *, page_size=100):
        self.activity_reads.append((session, page_size))
        return list(self.activities[session])


class WaitingReconciliationTests(unittest.TestCase):
    def test_only_waiting_sessions_get_activity_reads_and_new_question_is_detected(self):
        targets = (
            ProjectTarget("GS", "GS", "hamad933/GS-2", "GS_SHADOW_V2"),
            ProjectTarget(
                "CEP",
                "PERSONAL:CEP",
                "hamad933/Cybersecurity-Education-Platform",
                "CEP_SHADOW_V2",
            ),
        )
        client = FakeClient()
        result = reconcile_waiting_sessions(client=client, targets=targets)
        self.assertEqual(result["waiting_session_count"], 1)
        self.assertEqual(result["new_question_after_user_response_count"], 1)
        self.assertEqual(client.activity_reads, [("sessions/w04-private-id", 100)])
        item = result["waiting_sessions"][0]
        self.assertEqual(item["starting_branch"], "work/cep-w04-parent-reconciliation-r02")
        self.assertEqual(item["activity_kind_counts"], {"AGENT_MESSAGE": 2, "USER_MESSAGE": 1})
        self.assertEqual(item["latest_activity_kind"], "AGENT_MESSAGE")
        self.assertTrue(item["agent_question_after_latest_user_message"])
        self.assertTrue(item["new_waiting_activity_after_prior_user_response"])
        serialized = json.dumps(result, sort_keys=True)
        for secret_text in (
            "w04-private-id",
            "old private question",
            "old private answer",
            "new private question",
        ):
            self.assertNotIn(secret_text, serialized)
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["raw_message_content_emitted"])

    def test_waiting_with_no_prior_user_message_does_not_claim_new_question_after_response(self):
        client = FakeClient()
        client.activities["sessions/w04-private-id"] = [
            {
                "name": "sessions/w04-private-id/activities/q1",
                "agentMessaged": {"agentMessage": "private question"},
            }
        ]
        targets = (
            ProjectTarget(
                "CEP",
                "PERSONAL:CEP",
                "hamad933/Cybersecurity-Education-Platform",
                "CEP_SHADOW_V2",
            ),
        )
        result = reconcile_waiting_sessions(client=client, targets=targets)
        item = result["waiting_sessions"][0]
        self.assertTrue(item["agent_question_after_latest_user_message"])
        self.assertFalse(item["new_waiting_activity_after_prior_user_response"])


if __name__ == "__main__":
    unittest.main()
