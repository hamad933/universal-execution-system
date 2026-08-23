from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.provider_observer import ProjectTarget, load_project_targets, observe_provider_sessions
from ues.state_store import StateRead, WorkstreamRuntimeRecord


class FakeClient:
    def __init__(self):
        self.sessions = [
            {
                "name": "sessions/cep-waiting-secret-id",
                "normalizedState": "AWAITING_USER_FEEDBACK",
                "sourceIdentifier": "sources/cep",
                "title": "CEP-W04-R03: Progress & Evidence",
            },
            {
                "name": "sessions/gs-failed-secret-id",
                "normalizedState": "FAILED",
                "sourceIdentifier": "sources/gs",
                "title": "GS-G70-JULES-IPA-HOME-R1",
            },
            {
                "name": "sessions/other-secret-id",
                "normalizedState": "COMPLETED",
                "sourceIdentifier": "sources/other",
                "title": "Other project",
            },
            {
                "name": "sessions/unbound-secret-id",
                "normalizedState": "IN_PROGRESS",
                "sourceIdentifier": None,
                "title": "No source",
            },
        ]
        self.sources = {
            "sources/cep": {
                "explicitRepositoryIdentity": True,
                "repository": "hamad933/Cybersecurity-Education-Platform",
            },
            "sources/gs": {
                "explicitRepositoryIdentity": True,
                "repository": "hamad933/GS-2",
            },
            "sources/other": {
                "explicitRepositoryIdentity": True,
                "repository": "hamad933/Other",
            },
        }
        self.list_page_size = None

    def list_sessions(self, *, page_size=100):
        self.list_page_size = page_size
        return list(self.sessions)

    def get_source(self, source):
        return dict(self.sources[source])


class FakeStore:
    def __init__(self):
        self.records = {}
        self.versions = {}

    def read_workstream(self, lane_id):
        if lane_id not in self.records:
            return StateRead("MISSING", 0, None, "SHADOW", False, None, False)
        record = self.records[lane_id]
        return StateRead("OK", self.versions[lane_id], record, "SHADOW", False, None, False)

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        current_version = self.versions.get(lane_id, 0)
        if current_version != expected_version:
            raise AssertionError("unexpected test CAS conflict")
        self.records[lane_id] = WorkstreamRuntimeRecord.from_dict(record.to_dict())
        self.versions[lane_id] = current_version + 1
        return StateRead(
            "OK",
            self.versions[lane_id],
            self.records[lane_id],
            "SHADOW",
            False,
            None,
            False,
        )


class ProviderObserverTests(unittest.TestCase):
    def setUp(self):
        self.targets = (
            ProjectTarget("GS", "GS", "hamad933/GS-2", "GS_SHADOW_V2"),
            ProjectTarget(
                "CEP",
                "PERSONAL:CEP",
                "hamad933/Cybersecurity-Education-Platform",
                "CEP_SHADOW_V2",
            ),
        )

    def test_observer_ingests_all_provider_sessions_but_persists_only_monitored_exact_sources(self):
        client = FakeClient()
        store = FakeStore()
        result = observe_provider_sessions(client=client, store=store, targets=self.targets)

        self.assertEqual(client.list_page_size, 100)
        self.assertEqual(result["account_session_count"], 4)
        self.assertEqual(result["observed_monitored_session_count"], 2)
        self.assertEqual(result["outside_monitored_repository_count"], 1)
        self.assertEqual(result["unbound_source_count"], 1)
        self.assertEqual(result["attention_required_count"], 2)
        self.assertEqual(result["project_state_counts"]["CEP"], {"AWAITING_USER_FEEDBACK": 1})
        self.assertEqual(result["project_state_counts"]["GS"], {"FAILED": 1})
        self.assertEqual(len(store.records), 2)

        serialized = json.dumps(
            {lane: record.to_dict() for lane, record in store.records.items()},
            sort_keys=True,
        )
        self.assertNotIn("cep-waiting-secret-id", serialized)
        self.assertNotIn("gs-failed-secret-id", serialized)
        self.assertNotIn("CEP-W04-R03", serialized)
        self.assertNotIn("GS-G70-JULES-IPA-HOME-R1", serialized)
        self.assertIn("CONTROLLER_INPUT_RECONCILIATION_REQUIRED", serialized)
        self.assertIn("TERMINAL_FAILURE_RECONCILIATION_REQUIRED", serialized)
        self.assertIn("provider_mutation_authorized", serialized)

    def test_second_observation_updates_existing_lane_and_records_state_transition(self):
        client = FakeClient()
        store = FakeStore()
        observe_provider_sessions(client=client, store=store, targets=self.targets)
        client.sessions[0]["normalizedState"] = "IN_PROGRESS"
        result = observe_provider_sessions(client=client, store=store, targets=self.targets)
        self.assertEqual(result["project_state_counts"]["CEP"], {"IN_PROGRESS": 1})
        cep_record = next(
            record for record in store.records.values() if record.project == "CEP"
        )
        self.assertEqual(cep_record.last_observed_provider_state["state"], "IN_PROGRESS")
        self.assertEqual(cep_record.last_successful_transition["from"], "AWAITING_USER_FEEDBACK")
        self.assertEqual(cep_record.last_successful_transition["to"], "IN_PROGRESS")

    def test_adapter_loader_selects_provider_owned_project_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gs.json").write_text(
                json.dumps(
                    {
                        "adapter_id": "GS",
                        "project": "GS",
                        "route": "GS",
                        "repository": "hamad933/GS-2",
                        "truth_owners": {"provider_state": "PROVIDER"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "ignored.json").write_text(
                json.dumps(
                    {
                        "adapter_id": "IGNORED",
                        "project": "X",
                        "route": "X",
                        "repository": "hamad933/X",
                        "truth_owners": {"provider_state": "GITHUB"},
                    }
                ),
                encoding="utf-8",
            )
            targets = load_project_targets(root)
        self.assertEqual(
            targets,
            (ProjectTarget("GS", "GS", "hamad933/GS-2", "GS"),),
        )


if __name__ == "__main__":
    unittest.main()
