from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.lineage_effects import create_next_lineage_generation, send_same_lineage_message
from ues.lineage_registry import session_fingerprint, upsert_lineage_observation
from ues.state_store import DeterministicFileStateStore


class FakeLifecycleClient:
    def __init__(self) -> None:
        self.message_calls = 0
        self.create_calls = 0

    def send_message(self, session_name, prompt, *, expected_repository, expected_source):
        self.message_calls += 1
        return {
            "outcome": "DELIVERED",
            "activity": "sessions/1/activities/new",
            "repository": expected_repository,
        }

    def create_session(self, *, prompt, title, source, starting_branch, expected_repository):
        self.create_calls += 1
        return {
            "session": f"sessions/new-{self.create_calls}",
            "repository": expected_repository,
            "starting_branch": starting_branch,
            "state": "QUEUED",
        }


class LineageEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.fp = session_fingerprint("sessions/existing")
        upsert_lineage_observation(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W03",
            role="WRITER",
            binding={
                "status": "PROVEN",
                "reason": "EXACT",
                "session_fingerprint": self.fp,
                "provider_state": "AWAITING_USER_FEEDBACK",
                "session": {
                    "_source_repository": "owner/repo",
                    "sourceStartingBranch": "work/w03",
                },
            },
            policy={"known_session_fingerprints": [self.fp], "starting_branch": "work/w03"},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_same_lineage_message_is_idempotent_across_repeated_cycle(self) -> None:
        client = FakeLifecycleClient()
        args = dict(
            store=self.store,
            client=client,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W03",
            role="WRITER",
            session_name="sessions/existing",
            source_name="sources/github/owner/repo",
            repository="owner/repo",
            prompt="continue correction",
            trigger_fingerprint="trigger-1",
            authority_event_id="OWNER_EVENT",
            action="waiting-answer",
        )
        first = send_same_lineage_message(**args)
        second = send_same_lineage_message(**args)
        self.assertEqual(first["decision"], "SAME_LINEAGE_MESSAGE_CONFIRMED")
        self.assertEqual(client.message_calls, 1)
        self.assertEqual(second["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertFalse(second["provider_write_attempted"])

    def test_new_generation_denied_when_budget_not_proven(self) -> None:
        client = FakeLifecycleClient()
        result = create_next_lineage_generation(
            self.store,
            client,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W03",
            role="WRITER",
            predecessor_session_fingerprint=self.fp,
            next_generation=2,
            prompt="continue",
            title="W03 Writer G2",
            source_name="sources/github/owner/repo",
            starting_branch="work/w03",
            repository="owner/repo",
            authority_event_id="OWNER_EVENT",
            budget_safe=False,
        )
        self.assertEqual(result["decision"], "NEW_SESSION_BUDGET_NOT_PROVEN")
        self.assertEqual(client.create_calls, 0)

    def test_new_generation_is_idempotent_when_budget_and_exact_spec_are_proven(self) -> None:
        client = FakeLifecycleClient()
        args = dict(
            store=self.store,
            client=client,
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W03",
            role="WRITER",
            predecessor_session_fingerprint=self.fp,
            next_generation=2,
            prompt="continue",
            title="W03 Writer G2",
            source_name="sources/github/owner/repo",
            starting_branch="work/w03",
            repository="owner/repo",
            authority_event_id="OWNER_EVENT",
            budget_safe=True,
        )
        first = create_next_lineage_generation(**args)
        second = create_next_lineage_generation(**args)
        self.assertEqual(first["decision"], "NEXT_SESSION_GENERATION_CONFIRMED")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(second["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
