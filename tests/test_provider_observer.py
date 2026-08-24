from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ues.provider_observer import (
    PROJECTS,
    audit_provider_observation,
    collect_provider_observation,
    observation_lane_id,
    observation_manifest,
    persist_provider_observation,
)
from ues.state_store import StateRead, WorkstreamRuntimeRecord


class FakeJulesClient:
    def list_sources(self, *, page_size=100):
        self.source_page_size = page_size
        return [
            {
                "name": "sources/gs-source-secret-id",
                "repository": "hamad933/GS-2",
                "explicitRepositoryIdentity": True,
            },
            {
                "name": "sources/cep-source-secret-id",
                "repository": "hamad933/Cybersecurity-Education-Platform",
                "explicitRepositoryIdentity": True,
            },
            {
                "name": "sources/other",
                "repository": "hamad933/other",
                "explicitRepositoryIdentity": True,
            },
        ]

    def list_sessions(self, *, page_size=100):
        self.session_page_size = page_size
        return [
            {
                "name": "sessions/gs-raw-session-id",
                "title": "GS-G70-JULES-IPA-HOME-R1",
                "normalizedState": "FAILED",
                "stateAuthoritative": True,
                "sourceIdentifier": "sources/gs-source-secret-id",
                "sourceStartingBranch": "work/home",
            },
            {
                "name": "sessions/cep-raw-session-id",
                "title": "CEP-W04-R03: Progress & Evidence",
                "normalizedState": "AWAITING_USER_FEEDBACK",
                "stateAuthoritative": True,
                "sourceIdentifier": "sources/cep-source-secret-id",
                "sourceStartingBranch": "work/cep-w04",
            },
            {
                "name": "sessions/other-session-id",
                "title": "Other project",
                "normalizedState": "COMPLETED",
                "stateAuthoritative": True,
                "sourceIdentifier": "sources/other",
            },
        ]

    def list_activities(self, session, *, page_size=100):
        return [
            {
                "name": f"{session}/activities/raw-activity-id",
                "type": "USER_MESSAGE",
                "createTime": "2026-08-24T00:00:00Z",
                "prompt": "private activity body must never persist",
            }
        ]


class FakeStore:
    def __init__(self):
        self.records = {}
        self.versions = {}

    def read_workstream(self, lane_id):
        if lane_id not in self.records:
            return StateRead("MISSING", 0, None, "SHADOW", False, "missing", False)
        record = self.records[lane_id]
        return StateRead("OK", self.versions[lane_id], record, "SHADOW", False, None, False)

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        current = self.versions.get(lane_id, 0)
        if current != expected_version:
            raise AssertionError("unexpected fake CAS version")
        self.versions[lane_id] = current + 1
        copied = WorkstreamRuntimeRecord.from_dict(record.to_dict())
        self.records[lane_id] = copied
        return StateRead("OK", current + 1, copied, "SHADOW", False, None, False)


class ProviderObserverTests(unittest.TestCase):
    def test_collects_exact_source_project_state_without_persisting_raw_ids_or_activity_content(self):
        result = collect_provider_observation(
            FakeJulesClient(),
            observed_at="2026-08-24T00:10:00Z",
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertEqual(result["account_visible_session_count"], 3)
        self.assertEqual(result["unattributed_or_other_project_session_count"], 1)
        self.assertNotIn("gs-raw-session-id", serialized)
        self.assertNotIn("cep-raw-session-id", serialized)
        self.assertNotIn("raw-activity-id", serialized)
        self.assertNotIn("private activity body", serialized)
        self.assertNotIn("gs-source-secret-id", serialized)
        self.assertNotIn("cep-source-secret-id", serialized)
        gs = result["projects"]["GS"]
        cep = result["projects"]["CEP"]
        self.assertEqual(gs["session_count"], 1)
        self.assertEqual(cep["session_count"], 1)
        self.assertTrue(gs["attention_required"])
        self.assertTrue(cep["attention_required"])
        self.assertEqual(
            gs["sessions"][0]["classification"],
            "TERMINAL_FAILURE_REQUIRES_RECONCILIATION",
        )
        self.assertEqual(
            cep["sessions"][0]["classification"],
            "WAITING_INPUT_REQUIRES_RECONCILIATION",
        )
        self.assertEqual(gs["sessions"][0]["source_repository"], "hamad933/GS-2")
        self.assertTrue(gs["sessions"][0]["source_binding_proven"])
        self.assertFalse(gs["sessions"][0]["raw_session_id_persisted"])
        self.assertFalse(gs["sessions"][0]["activity_content_persisted"])

    def test_manifest_uses_deterministic_project_observation_refs(self):
        manifest = observation_manifest()
        self.assertEqual(len(manifest["projects"]), 2)
        refs = {item["project"]: item["state_ref"] for item in manifest["projects"]}
        self.assertEqual(
            refs["GS"],
            "ues-runtime/v2/lane/df79cc1b5ac694ed134f43ded3becf60831cdd276af859a601c1e23fa652f0ae",
        )
        self.assertEqual(
            refs["CEP"],
            "ues-runtime/v2/lane/d390fd737a91469956068ba284693815ba58b5f538c62f08c7007be930013eed",
        )

    def test_persisted_project_observation_is_shadow_and_never_grants_provider_mutation(self):
        snapshot = collect_provider_observation(
            FakeJulesClient(),
            observed_at="2026-08-24T00:10:00Z",
        )
        store = FakeStore()
        with patch("ues.provider_observer.build_live_state_store", return_value=store):
            persisted = persist_provider_observation(snapshot)
        self.assertEqual(len(persisted["saved"]), 2)
        for project in PROJECTS:
            record = store.records[observation_lane_id(project)]
            self.assertEqual(record.activation_mode, "SHADOW")
            self.assertEqual(record.actor_bindings, {})
            self.assertFalse(record.authority_provenance["provider_mutation_authorized"])
            self.assertEqual(record.authority_provenance["scope"], "READ_ONLY_PROVIDER_OBSERVATION")

    def test_attention_is_reported_without_turning_normal_completed_or_waiting_state_into_hard_failure(self):
        snapshot = collect_provider_observation(
            FakeJulesClient(),
            observed_at="2026-08-24T00:10:00Z",
        )
        store = FakeStore()
        with patch("ues.provider_observer.build_live_state_store", return_value=store):
            persist_provider_observation(snapshot)
            result = audit_provider_observation(
                now=datetime(2026, 8, 24, 0, 20, tzinfo=timezone.utc),
                stale_seconds=2700,
            )
        self.assertEqual(result["cycle_status"], "PROVIDER_OBSERVER_OK")
        self.assertTrue(result["attention_required"])
        self.assertFalse(result["hard_incidents"])
        projects = {item["project"]: item for item in result["projects"]}
        self.assertFalse(projects["GS"]["stale"])
        self.assertFalse(projects["CEP"]["stale"])

    def test_stale_observation_is_a_hard_watchdog_failure(self):
        snapshot = collect_provider_observation(
            FakeJulesClient(),
            observed_at="2026-08-24T00:00:00Z",
        )
        store = FakeStore()
        with patch("ues.provider_observer.build_live_state_store", return_value=store):
            persist_provider_observation(snapshot)
            result = audit_provider_observation(
                now=datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),
                stale_seconds=2700,
            )
        self.assertEqual(result["cycle_status"], "PROVIDER_OBSERVER_HARD_FAILURE")
        self.assertEqual(
            {item["code"] for item in result["hard_incidents"]},
            {"PROVIDER_OBSERVATION_STALE"},
        )


if __name__ == "__main__":
    unittest.main()
