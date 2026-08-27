from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues import evidence_supplement_terminal as target
from ues.lineage_registry import session_fingerprint


class Store:
    def __init__(self):
        self.record = SimpleNamespace(evidence_bindings={
            "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "role": "ASSURANCE",
            "generation": 1,
            "session_fingerprint": session_fingerprint("sessions/abc"),
            "source_repository": "sha256:" + "a" * 64,
            "provider_starting_branch": "evidence-branch",
            "current_candidate_sha": "0" * 40,
        })
    def read_workstream(self, lane_id):
        return SimpleNamespace(status="OK", record=self.record, version=1)


class Client:
    def __init__(self, state="IN_PROGRESS"):
        self.state = state
        self.activity_calls = 0
    def list_sessions(self, page_size=100):
        return [{
            "name": "sessions/abc", "normalizedState": self.state,
            "sourceIdentifier": "sources/private", "sourceStartingBranch": "evidence-branch",
        }]
    def list_activities(self, session, page_size=100):
        self.activity_calls += 1
        return [{"agentMessaged": {"agentMessage": "unused"}}]


class EvidenceSupplementTerminalTests(unittest.TestCase):
    def test_nonterminal_is_read_only_and_does_not_read_activities(self):
        client = Client("IN_PROGRESS")
        with patch.object(target, "_resolve_unique_source", return_value=("sources/private", "private/repo")):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", store=Store(), client=client)
        self.assertEqual(result["result"], "SUPPLEMENT_SESSION_NONTERMINAL")
        self.assertEqual(result["provider_state"], "IN_PROGRESS")
        self.assertEqual(client.activity_calls, 0)
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_completed_exact_binding_materializes_and_persists(self):
        client = Client("COMPLETED")
        candidate = {
            "structured": True, "role": "ASSURANCE",
            "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "status": "COMPLETE", "verdict": "PASS", "candidate_sha": "0" * 40,
            "reviewed_sha": "0" * 40, "finding_count": 0, "findings": [],
        }
        bound = {
            "logical_workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "role": "ASSURANCE", "generation": 1,
            "session_fingerprint": session_fingerprint("sessions/abc"),
            "result_state": "PARENT_CONSUMABLE", "verdict": "PASS",
            "finding_count": 0, "findings": [], "result_fingerprint": "b" * 64,
        }
        with patch.object(target, "_resolve_unique_source", return_value=("sources/private", "private/repo")), \
             patch.object(target, "extract_terminal_candidate_with_legacy_recovery", return_value=candidate), \
             patch.object(target, "_bound_result", return_value=bound), \
             patch.object(target, "persist_terminal_result", return_value={"state":"TERMINAL_RESULT_PERSISTED","authoritative_readback":True}):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", store=Store(), client=client)
        self.assertEqual(result["provider_state"], "COMPLETED")
        self.assertEqual(result["terminal_result"]["result_state"], "PARENT_CONSUMABLE")
        self.assertEqual(result["persistence"]["state"], "TERMINAL_RESULT_PERSISTED")
        self.assertFalse(result["private_source_identity_persisted"])
        self.assertEqual(client.activity_calls, 1)

    def test_rejects_unbound_or_non_supplement_lane(self):
        store = Store()
        store.record.evidence_bindings["source_repository"] = "hamad/repo"
        with self.assertRaises(ValueError):
            target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", store=store, client=Client())


if __name__ == "__main__":
    unittest.main()
