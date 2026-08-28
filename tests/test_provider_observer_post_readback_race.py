from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ues.provider_observer import PROJECTS, observation_lane_id
from ues.provider_observer_recovery import _health_lane_id, recover_if_stale
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord


NOW = datetime(2026, 8, 27, 4, 10, tzinfo=timezone.utc)


def _store() -> DeterministicFileStateStore:
    directory = tempfile.TemporaryDirectory()
    store = DeterministicFileStateStore(Path(directory.name) / "state.json")
    store.initialize()
    store._test_directory = directory
    return store


def _seed_observation(store, project, observed_at: str) -> None:
    lane_id = observation_lane_id(project)
    read = store.read_workstream(lane_id)
    record = WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project=project["project"],
        route=project["route"],
        workstream_id="PROVIDER-OBSERVATION",
        activation_mode="SHADOW",
        authority_provenance={"provider_mutation_authorized": False},
        last_observed_provider_state={
            "provider": "JULES",
            "provider_read_complete": True,
            "provider_mutation_performed": False,
            "observed_at": observed_at,
        },
    )
    expected = 0 if read.status == "MISSING" else read.version
    store.compare_and_swap_workstream(lane_id, expected, record)


class ProviderObserverPostReadbackRaceTests(unittest.TestCase):
    def test_local_failure_is_superseded_when_authoritative_post_readback_is_fresh(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-27T03:00:00Z")

        def failed_local_observer_after_concurrent_success():
            for project in PROJECTS:
                _seed_observation(store, project, "2026-08-27T04:10:00Z")
            return {
                "result": "JULES_PROVIDER_OBSERVATION_FAILED",
                "error_category": "NETWORK_ERROR",
                "provider_mutation_performed": False,
                "new_tasks_or_sessions_created": 0,
            }

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch(
                "ues.provider_observer_recovery.observe",
                side_effect=failed_local_observer_after_concurrent_success,
            ) as observer,
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK")
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["after_lease"]["recovery_required"])
        self.assertEqual(result["observation"]["error_category"], "NETWORK_ERROR")
        observer.assert_called_once()

        health = store.read_workstream(_health_lane_id())
        self.assertEqual(health.status, "OK")
        assert health.record is not None
        state = health.record.last_observed_provider_state or {}
        self.assertEqual(state["status"], "PASS")
        self.assertEqual(state["result"], "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK")
        self.assertFalse(state["recovery_required"])

    def test_local_failure_remains_failed_when_authoritative_post_readback_is_stale(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-27T03:00:00Z")

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch(
                "ues.provider_observer_recovery.observe",
                return_value={
                    "result": "JULES_PROVIDER_OBSERVATION_FAILED",
                    "error_category": "NETWORK_ERROR",
                    "provider_mutation_performed": False,
                    "new_tasks_or_sessions_created": 0,
                },
            ),
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_FAILED")
        self.assertTrue(result["after"]["recovery_required"])
        self.assertFalse(result["provider_mutation_performed"])

        health = store.read_workstream(_health_lane_id())
        self.assertEqual(health.status, "OK")
        assert health.record is not None
        state = health.record.last_observed_provider_state or {}
        self.assertEqual(state["status"], "FAIL")
        self.assertEqual(state["result"], "PROVIDER_OBSERVER_FALLBACK_FAILED")
        self.assertTrue(state["recovery_required"])


if __name__ == "__main__":
    unittest.main()
