from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ues.identity import canonical_lane_id
from ues.provider_observer import PROJECTS, observation_lane_id
from ues.provider_observer_recovery import (
    RECOVERY_LEASE_TTL_SECONDS,
    RECOVERY_OWNER,
    freshness_snapshot,
    recover_if_stale,
)
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord


NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def _store() -> DeterministicFileStateStore:
    directory = tempfile.TemporaryDirectory()
    store = DeterministicFileStateStore(Path(directory.name) / "state.json")
    store.initialize()
    store._test_directory = directory  # keep directory alive for the test lifetime
    return store


def _seed_observation(store, project, observed_at: str) -> None:
    lane_id = observation_lane_id(project)
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
    store.compare_and_swap_workstream(lane_id, 0, record)


class ProviderObserverRecoveryTests(unittest.TestCase):
    def test_fresh_observations_do_not_trigger_provider_read(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-24T07:50:00Z")

        snapshot = freshness_snapshot(store, now=NOW, stale_seconds=20 * 60)
        self.assertFalse(snapshot["recovery_required"])

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch("ues.provider_observer_recovery.observe") as observer,
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED")
        observer.assert_not_called()

    def test_stale_observation_runs_one_read_only_recovery_and_releases_lease(self):
        store = _store()
        _seed_observation(store, PROJECTS[0], "2026-08-24T07:10:00Z")
        _seed_observation(store, PROJECTS[1], "2026-08-24T07:55:00Z")

        def fake_observe():
            return {
                "result": "JULES_PROVIDER_OBSERVATION_COMPLETE",
                "provider_mutation_performed": False,
            }

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch("ues.provider_observer_recovery.observe", side_effect=fake_observe) as observer,
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_RECOVERED")
        self.assertFalse(result["provider_mutation_performed"])
        observer.assert_called_once()
        health_lane = canonical_lane_id("UES", "INTERNAL:UES", "PROVIDER-OBSERVER-HEALTH")
        health = store.read_workstream(health_lane)
        self.assertEqual(health.status, "OK")
        self.assertIsNotNone(health.record)
        assert health.record is not None
        self.assertIsNone(health.record.lease)

    def test_existing_recovery_lease_suppresses_duplicate_observer_call(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-24T07:00:00Z")

        health_lane = canonical_lane_id("UES", "INTERNAL:UES", "PROVIDER-OBSERVER-HEALTH")
        health_record = WorkstreamRuntimeRecord(
            lane_id=health_lane,
            project="UES",
            route="INTERNAL:UES",
            workstream_id="PROVIDER-OBSERVER-HEALTH",
            activation_mode="SHADOW",
        )
        store.compare_and_swap_workstream(health_lane, 0, health_record)
        store.acquire_lease(
            health_lane,
            RECOVERY_OWNER,
            "existing-observer-recovery",
            RECOVERY_LEASE_TTL_SECONDS,
            now=NOW,
        )

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch("ues.provider_observer_recovery.observe") as observer,
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_ALREADY_IN_FLIGHT")
        observer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
