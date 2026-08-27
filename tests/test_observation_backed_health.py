from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ues.identity import canonical_lane_id
from ues.lifecycle_runtime_observed import _promote_effect_counts
from ues.observation_backed_health import (
    _latest_confirmed_lineage_effect_at,
    observation_backed_no_effect_eligible,
    run_observation_backed_no_effect_health,
)
from ues.provider_observer import OBSERVATION_WORKSTREAM
from ues.rp_readonly_runtime import _load_rp_adapter
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord


class _DiscoverableFileStateStore(DeterministicFileStateStore):
    """Expose the production discovery contract over the deterministic test document."""

    def discover_lane_ids(self) -> tuple[str, ...]:
        doc = self._read_doc()
        return tuple(sorted(str(item) for item in doc["workstreams"]))


def _store() -> _DiscoverableFileStateStore:
    directory = tempfile.TemporaryDirectory()
    store = _DiscoverableFileStateStore(Path(directory.name) / "state.json")
    store.initialize()
    store._test_directory = directory
    return store


def _put(store, record: WorkstreamRuntimeRecord) -> None:
    read = store.read_workstream(record.lane_id)
    expected = 0 if read.status == "MISSING" else read.version
    store.compare_and_swap_workstream(record.lane_id, expected, record)


class ObservationBackedHealthTests(unittest.TestCase):
    def test_empty_rp_topology_is_eligible(self):
        adapter = _load_rp_adapter("RP01")
        authority = {
            "lineages": {},
            "generation_policy": {
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
            "workflow_dispatches": {},
        }
        self.assertTrue(observation_backed_no_effect_eligible(adapter, authority))

    def test_any_effect_capable_topology_forces_live_runtime(self):
        adapter = _load_rp_adapter("RP01")
        cases = [
            {"lineages": {"W01": {"writer": {}}}},
            {"generation_policy": {"authorized_initial_lineages": {"W01:WRITER": {"authorized": True}}}},
            {"generation_policy": {"authorized_lineages": {"W01:WRITER": {"authorized": True}}}},
            {"workflow_dispatches": {"W01": {"authorized": True}}},
        ]
        for authority in cases:
            with self.subTest(authority=authority):
                self.assertFalse(observation_backed_no_effect_eligible(adapter, authority))

    def test_effect_counters_are_promoted_without_guessing(self):
        result = {
            "summary": {
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
            }
        }
        promoted = _promote_effect_counts(result)
        self.assertEqual(promoted["external_effects_dispatched"], 0)
        self.assertEqual(promoted["new_tasks_or_sessions_created"], 0)

        missing = _promote_effect_counts({"summary": {}})
        self.assertNotIn("external_effects_dispatched", missing)
        self.assertNotIn("new_tasks_or_sessions_created", missing)

    def test_young_observation_predating_generation_is_rejected_from_result_merge(self):
        store = _store()
        project = "RP04"
        route = "RP04"
        repository = "hamad933/Real-Estate-Assets-Control-"
        observation_lane = canonical_lane_id(project, route, OBSERVATION_WORKSTREAM)
        _put(
            store,
            WorkstreamRuntimeRecord(
                lane_id=observation_lane,
                project=project,
                route=route,
                workstream_id=OBSERVATION_WORKSTREAM,
                activation_mode="SHADOW",
                last_observed_provider_state={
                    "provider": "JULES",
                    "provider_read_complete": True,
                    "provider_mutation_performed": False,
                    "repository": repository,
                    "observed_at": "2026-08-27T15:46:00Z",
                    "session_count": 1,
                    "results": [
                        {
                            "session_fingerprint": "old-session",
                            "logical_workstream": None,
                            "role": None,
                            "generation": None,
                            "result_state": "RESULT_IDENTITY_UNRESOLVED",
                        }
                    ],
                },
            ),
        )
        lineage_lane = "ues-lane:v1|RP04|RP04|LINEAGE%3A%3ARP04-IPA-S01-001%3A%3AREVIEWER"
        _put(
            store,
            WorkstreamRuntimeRecord(
                lane_id=lineage_lane,
                project=project,
                route=route,
                workstream_id="LINEAGE::RP04-IPA-S01-001::REVIEWER",
                activation_mode="SHADOW",
                operation_receipt={
                    "state": "CONFIRMED",
                    "generation": 4,
                    "post_condition": {
                        "observed": True,
                        "read_at": "2026-08-27T15:50:24Z",
                    },
                },
            ),
        )

        latest = _latest_confirmed_lineage_effect_at(store, project=project, route=route)
        self.assertEqual(latest, datetime(2026, 8, 27, 15, 50, 24, tzinfo=timezone.utc))

        with (
            patch("ues.observation_backed_health.build_live_state_store", return_value=store),
            patch("ues.observation_backed_health.read_persisted_terminal_results", return_value=[]),
        ):
            result = run_observation_backed_no_effect_health(
                _load_rp_adapter(project),
                authority=None,
                now=datetime(2026, 8, 27, 15, 55, tzinfo=timezone.utc),
            )

        summary = result["summary"]
        self.assertTrue(summary["provider_observation_available"])
        self.assertFalse(summary["provider_observation_fresh"])
        self.assertEqual(
            summary["provider_observation_reason"],
            "PROVIDER_OBSERVATION_PREDATES_LATEST_LINEAGE_EFFECT",
        )
        self.assertEqual(
            summary["provider_inventory_source"],
            "STATESTORE_PROVIDER_OBSERVATION_REJECTED_AS_STALE_RELATIVE_TO_LINEAGE",
        )
        self.assertEqual(result["results"], [])


if __name__ == "__main__":
    unittest.main()
