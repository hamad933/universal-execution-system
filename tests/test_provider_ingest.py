from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ues.provider_ingest import ingest_provider_artifacts
from ues.provider_targets import ProjectTarget, load_project_targets
from ues.state_store import StateRead, WorkstreamRuntimeRecord


class FakeStore:
    def __init__(self):
        self.records = {}
        self.versions = {}

    def read_workstream(self, lane_id):
        if lane_id not in self.records:
            return StateRead("MISSING", 0, None, "SHADOW", False, None, False)
        return StateRead(
            "OK",
            self.versions[lane_id],
            self.records[lane_id],
            "SHADOW",
            False,
            None,
            False,
        )

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        current = self.versions.get(lane_id, 0)
        if current != expected_version:
            raise AssertionError("unexpected fake-store CAS conflict")
        self.records[lane_id] = WorkstreamRuntimeRecord.from_dict(record.to_dict())
        self.versions[lane_id] = current + 1
        return StateRead(
            "OK",
            self.versions[lane_id],
            self.records[lane_id],
            "SHADOW",
            False,
            None,
            False,
        )


TARGETS = (
    ProjectTarget(
        "CEP",
        "PERSONAL:CEP",
        "hamad933/Cybersecurity-Education-Platform",
        "CEP_SHADOW_V2",
    ),
)
SESSION_HASH = "a" * 64
SOURCE_HASH = "b" * 64
TITLE_HASH = "c" * 64
QUESTION_HASH = "d" * 64
USER_HASH = "e" * 64
AGENT_ACTIVITY_HASH = "1" * 64
USER_ACTIVITY_HASH = "2" * 64


def inventory():
    return {
        "schema_version": "2.1",
        "result": "LIVE_PROVIDER_INVENTORY_PASS",
        "provider": "JULES",
        "provider_mutation_performed": False,
        "raw_session_identity_emitted": False,
        "raw_title_emitted": False,
        "secret_material_emitted": False,
        "observations": [
            {
                "project": "CEP",
                "route": "PERSONAL:CEP",
                "repository": "hamad933/Cybersecurity-Education-Platform",
                "starting_branch": "work/cep-w04-parent-reconciliation-r02",
                "state": "AWAITING_USER_FEEDBACK",
                "classification": "CONTROLLER_INPUT_RECONCILIATION_REQUIRED",
                "session_identity_hash": SESSION_HASH,
                "source_identity_hash": SOURCE_HASH,
                "title_digest": TITLE_HASH,
                "raw_session_identity_emitted": False,
                "raw_title_emitted": False,
            }
        ],
    }


def waiting():
    return {
        "schema_version": "2.1",
        "result": "LIVE_WAITING_ACTIVITY_RECONCILIATION_PASS",
        "provider": "JULES",
        "provider_mutation_performed": False,
        "raw_session_identity_emitted": False,
        "raw_activity_identity_emitted": False,
        "raw_message_content_emitted": False,
        "secret_material_emitted": False,
        "waiting_sessions": [
            {
                "project": "CEP",
                "route": "PERSONAL:CEP",
                "repository": "hamad933/Cybersecurity-Education-Platform",
                "starting_branch": "work/cep-w04-parent-reconciliation-r02",
                "session_identity_hash": SESSION_HASH,
                "latest_activity_kind": "AGENT_MESSAGE",
                "latest_agent_activity_hash": AGENT_ACTIVITY_HASH,
                "latest_agent_question_digest": QUESTION_HASH,
                "latest_user_activity_hash": USER_ACTIVITY_HASH,
                "latest_user_message_digest": USER_HASH,
                "new_waiting_activity_after_prior_user_response": True,
                "agent_question_after_latest_user_message": True,
                "activity_count": 401,
                "raw_activity_identity_emitted": False,
                "raw_message_content_emitted": False,
            }
        ],
    }


class ProviderIngestTests(unittest.TestCase):
    def test_sanitized_artifact_persists_durable_waiting_lane_without_raw_provider_material(self):
        store = FakeStore()
        result = ingest_provider_artifacts(inventory(), waiting(), store=store, targets=TARGETS)
        self.assertEqual(result["persisted_session_count"], 1)
        self.assertEqual(result["state_counts"], {"AWAITING_USER_FEEDBACK": 1})
        self.assertFalse(result["provider_secret_present_in_ingest_process"])
        record = next(iter(store.records.values()))
        self.assertEqual(record.last_observed_provider_state["state"], "AWAITING_USER_FEEDBACK")
        self.assertEqual(
            record.last_observed_provider_state["latest_agent_question_digest"],
            QUESTION_HASH,
        )
        self.assertTrue(
            record.last_observed_provider_state["new_waiting_activity_after_prior_user_response"]
        )
        self.assertEqual(
            record.actor_bindings["PROVIDER_SESSION"]["role"],
            "UNCLASSIFIED_UNTIL_PROJECT_AUTHORITY_BINDS",
        )
        self.assertFalse(record.actor_bindings["PROVIDER_SESSION"]["mutation_authorized"])
        serialized = json.dumps(record.to_dict(), sort_keys=True)
        self.assertNotIn("JULES_API_KEY", serialized)
        self.assertNotIn("sessions/", serialized)
        self.assertNotIn("private question", serialized)

    def test_unsanitized_or_inconsistent_artifact_fails_closed_before_write(self):
        for mutate in (
            lambda inv, wait: inv.__setitem__("raw_session_identity_emitted", True),
            lambda inv, wait: inv["observations"][0].__setitem__("classification", "CONTINUE_PROVIDER_OBSERVATION"),
            lambda inv, wait: wait.__setitem__("raw_message_content_emitted", True),
        ):
            with self.subTest(mutate=mutate):
                inv, wait = copy.deepcopy(inventory()), copy.deepcopy(waiting())
                mutate(inv, wait)
                store = FakeStore()
                with self.assertRaises(ValueError):
                    ingest_provider_artifacts(inv, wait, store=store, targets=TARGETS)
                self.assertEqual(store.records, {})

    def test_adapter_loader_selects_only_provider_owned_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cep.json").write_text(
                json.dumps(
                    {
                        "adapter_id": "CEP",
                        "project": "CEP",
                        "route": "PERSONAL:CEP",
                        "repository": "hamad933/Cybersecurity-Education-Platform",
                        "truth_owners": {"provider_state": "PROVIDER"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "ignored.json").write_text(
                json.dumps(
                    {
                        "adapter_id": "X",
                        "project": "X",
                        "route": "X",
                        "repository": "hamad933/X",
                        "truth_owners": {"provider_state": "GITHUB"},
                    }
                ),
                encoding="utf-8",
            )
            targets = load_project_targets(root)
        self.assertEqual(targets, TARGETS)


if __name__ == "__main__":
    unittest.main()
