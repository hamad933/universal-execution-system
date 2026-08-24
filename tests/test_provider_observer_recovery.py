from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ues.provider_observer import PROJECTS, observation_lane_id
from ues.provider_observer_recovery import (
    RECOVERY_COORDINATION_WORKSTREAM,
    RECOVERY_HEALTH_WORKSTREAM,
    RECOVERY_LEASE_TTL_SECONDS,
    RECOVERY_OWNER,
    _coordination_lane_id,
    _health_lane_id,
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


class ProviderObserverRecoveryTests(unittest.TestCase):
    def test_fresh_observations_do_not_trigger_provider_read_and_persist_heartbeat(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-24T07:50:00Z")

        snapshot = freshness_snapshot(store, now=NOW, stale_seconds=20 * 60)
        self.assertFalse(snapshot["recovery_required"])

        env = {
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF": "refs/heads/main",
        }
        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch("ues.provider_observer_recovery.observe") as observer,
            patch.dict("os.environ", env, clear=False),
        ):
            result = recover_if_stale(now=NOW, stale_seconds=20 * 60)

        self.assertEqual(result["result"], "PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED")
        observer.assert_not_called()
        health = store.read_workstream(_health_lane_id())
        self.assertEqual(health.status, "OK")
        self.assertIsNotNone(health.record)
        assert health.record is not None
        state = health.record.last_observed_provider_state or {}
        self.assertEqual(state["result"], "PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED")
        self.assertEqual(state["trigger"]["event_name"], "schedule")
        self.assertEqual(state["trigger"]["run_id"], "12345")
        self.assertEqual(state["trigger"]["sha"], "a" * 40)
        self.assertFalse(state["provider_mutation_performed"])

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
        coordination = store.read_workstream(_coordination_lane_id())
        self.assertEqual(coordination.status, "OK")
        self.assertIsNotNone(coordination.record)
        assert coordination.record is not None
        self.assertIsNone(coordination.record.lease)
        health = store.read_workstream(_health_lane_id())
        self.assertEqual(health.status, "OK")
        self.assertEqual(
            (health.record.last_observed_provider_state or {}).get("result"),
            "PROVIDER_OBSERVER_FALLBACK_RECOVERED",
        )

    def test_existing_recovery_lease_suppresses_duplicate_observer_call(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-24T07:00:00Z")

        coordination_lane = _coordination_lane_id()
        coordination_record = WorkstreamRuntimeRecord(
            lane_id=coordination_lane,
            project="UES",
            route="INTERNAL:UES",
            workstream_id=RECOVERY_COORDINATION_WORKSTREAM,
            activation_mode="SHADOW",
        )
        store.compare_and_swap_workstream(coordination_lane, 0, coordination_record)
        store.acquire_lease(
            coordination_lane,
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
        health = store.read_workstream(_health_lane_id())
        self.assertEqual(health.status, "OK")
        self.assertEqual(
            (health.record.last_observed_provider_state or {}).get("result"),
            "PROVIDER_OBSERVER_FALLBACK_ALREADY_IN_FLIGHT",
        )

    def test_second_stale_period_can_recover_again_after_prior_recovery(self):
        store = _store()
        for project in PROJECTS:
            _seed_observation(store, project, "2026-08-24T07:00:00Z")

        with (
            patch("ues.provider_observer_recovery.build_live_state_store", return_value=store),
            patch(
                "ues.provider_observer_recovery.observe",
                return_value={
                    "result": "JULES_PROVIDER_OBSERVATION_COMPLETE",
                    "provider_mutation_performed": False,
                },
            ) as observer,
        ):
            first = recover_if_stale(now=NOW, stale_seconds=20 * 60)
            second = recover_if_stale(now=NOW + timedelta(minutes=25), stale_seconds=20 * 60)

        self.assertEqual(first["result"], "PROVIDER_OBSERVER_FALLBACK_RECOVERED")
        self.assertEqual(second["result"], "PROVIDER_OBSERVER_FALLBACK_RECOVERED")
        self.assertEqual(observer.call_count, 2)
        coordination = store.read_workstream(_coordination_lane_id())
        self.assertIsNone(coordination.record.lease)
        health = store.read_workstream(_health_lane_id())
        self.assertGreaterEqual(health.version, 2)
        self.assertEqual(health.record.workstream_id, RECOVERY_HEALTH_WORKSTREAM)


if __name__ == "__main__":
    unittest.main()
