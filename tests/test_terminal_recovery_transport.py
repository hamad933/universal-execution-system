from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues.observation_backed_health import run_observation_backed_no_effect_health
from ues.state_backends.github_refs import GitHubRefTransportError
from ues.state_backends.public_same_repo import OwnerAuthorizedSameRepoGitDataTransport
from ues.state_backends.recovery_same_repo import RecoverySameRepoGitDataTransport


class _MissingObservationStore:
    def read_workstream(self, lane_id):
        return SimpleNamespace(status="MISSING", record=None, version=0, reason=None)


class TerminalRecoveryTransportTests(unittest.TestCase):
    def test_metadata_preflight_failure_can_fall_back_to_exact_repo_git_data_read(self):
        transport = RecoverySameRepoGitDataTransport.__new__(RecoverySameRepoGitDataTransport)
        transport._storage_policy_verified = False
        transport.storage_visibility = "UNVERIFIED"
        calls = []

        def retry(action):
            calls.append("bounded-read")
            return action()

        transport._retry_throttled_read = retry
        transport._list_refs_once = lambda prefix: {} if prefix == "heads/ues-runtime/" else None
        with patch.object(
            OwnerAuthorizedSameRepoGitDataTransport,
            "assert_private_repository",
            side_effect=GitHubRefTransportError("GitHub repository metadata read failed (HTTP 403)"),
        ):
            transport.assert_private_repository()
        self.assertTrue(transport._storage_policy_verified)
        self.assertEqual(transport.storage_visibility, "OWNER_AUTHORIZED_SAME_REPOSITORY_CONTEXT")
        self.assertEqual(calls, ["bounded-read"])

    def test_metadata_and_git_data_preflight_failure_remains_fail_closed(self):
        transport = RecoverySameRepoGitDataTransport.__new__(RecoverySameRepoGitDataTransport)
        transport._storage_policy_verified = False
        transport.storage_visibility = "UNVERIFIED"

        def unavailable(action):
            raise GitHubRefTransportError("GitHub matching-ref read failed (HTTP 403)")

        transport._retry_throttled_read = unavailable
        transport._list_refs_once = lambda prefix: {}
        with (
            patch.object(
                OwnerAuthorizedSameRepoGitDataTransport,
                "assert_private_repository",
                side_effect=GitHubRefTransportError("GitHub repository metadata read failed (HTTP 403)"),
            ),
            self.assertRaises(GitHubRefTransportError),
        ):
            transport.assert_private_repository()
        self.assertFalse(transport._storage_policy_verified)
        self.assertEqual(transport.storage_visibility, "UNVERIFIED")

    def test_lifecycle_returns_durable_parent_result_when_aggregate_observation_is_missing(self):
        persisted = [{
            "project": "RP02",
            "route": "RP02",
            "logical_workstream": "S01",
            "role": "REVIEWER",
            "generation": 1,
            "session_fingerprint": "fingerprint-only",
            "repository": "hamad933/Enterprise-Operations-Control",
            "status": "COMPLETE",
            "verdict": "PASS",
            "reviewed_sha": "a" * 40,
            "finding_count": 0,
            "findings": [],
            "result_state": "PARENT_CONSUMABLE",
            "freshness_status": "FRESH",
        }]
        adapter = {
            "project": "RP02",
            "route": "RP02",
            "repository": "hamad933/Enterprise-Operations-Control",
        }

        def persist(store, *, project, route, status, summary):
            return {"status": status, "project": project, "route": route}

        with (
            patch("ues.observation_backed_health.build_live_state_store", return_value=_MissingObservationStore()),
            patch("ues.observation_backed_health.read_persisted_terminal_results", return_value=persisted),
            patch("ues.observation_backed_health.observation_backed_no_effect_eligible", return_value=True),
            patch("ues.observation_backed_health.observed.runtime_binding_from_env", return_value={}),
            patch("ues.observation_backed_health.observed._persist_health_with_runtime_binding", return_value=persist),
        ):
            result = run_observation_backed_no_effect_health(adapter, authority=None)
        self.assertEqual(result["results"], persisted)
        self.assertEqual(result["summary"]["parent_consumable_result_count"], 1)
        self.assertFalse(result["summary"]["provider_observation_available"])
        self.assertEqual(result["summary"]["provider_inventory_source"], "STATESTORE_DURABLE_LINEAGE_RESULTS_ONLY")
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)


if __name__ == "__main__":
    unittest.main()
