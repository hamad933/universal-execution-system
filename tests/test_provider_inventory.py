from __future__ import annotations

import json
import unittest

from ues.provider_inventory import inventory_provider_sessions
from ues.provider_observer import ProjectTarget


class FakeClient:
    def __init__(self):
        self.sessions = [
            {
                "name": "sessions/cep-secret-id",
                "normalizedState": "AWAITING_USER_FEEDBACK",
                "sourceIdentifier": "sources/cep",
                "title": "CEP-W04-R03: hidden title",
            },
            {
                "name": "sessions/gs-secret-id",
                "normalizedState": "COMPLETED",
                "sourceIdentifier": "sources/gs",
                "title": "GS hidden title",
            },
        ]
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
        self.session_page_size = None
        self.source_page_size = None

    def list_sessions(self, *, page_size=100):
        self.session_page_size = page_size
        return list(self.sessions)

    def list_sources(self, *, page_size=100):
        self.source_page_size = page_size
        return list(self.sources)


class ProviderInventoryTests(unittest.TestCase):
    def test_inventory_is_read_only_sanitized_project_partitioned_and_batched(self):
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
        result = inventory_provider_sessions(client=client, targets=targets)
        self.assertEqual(client.session_page_size, 100)
        self.assertEqual(client.source_page_size, 100)
        self.assertEqual(
            result["provider_read_shape"],
            "ONE_PAGINATED_SESSION_LIST_PLUS_ONE_PAGINATED_SOURCE_LIST",
        )
        self.assertEqual(result["account_session_count"], 2)
        self.assertEqual(result["provider_source_count"], 2)
        self.assertEqual(result["monitored_session_count"], 2)
        self.assertEqual(result["attention_required_count"], 2)
        self.assertEqual(result["project_state_counts"]["CEP"], {"AWAITING_USER_FEEDBACK": 1})
        self.assertEqual(result["project_state_counts"]["GS"], {"COMPLETED": 1})
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("cep-secret-id", serialized)
        self.assertNotIn("gs-secret-id", serialized)
        self.assertNotIn("CEP-W04-R03", serialized)
        self.assertNotIn("GS hidden title", serialized)
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["raw_session_identity_emitted"])
        self.assertFalse(result["raw_title_emitted"])


if __name__ == "__main__":
    unittest.main()
