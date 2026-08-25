from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues.observation_backed_health import run_observation_backed_no_effect_health


class _MissingObservationStore:
    def read_workstream(self, lane_id):
        return SimpleNamespace(status="MISSING", record=None, version=0, reason=None)


class TerminalRecoveryLifecycleTests(unittest.TestCase):
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
        self.assertEqual(
            result["summary"]["provider_inventory_source"],
            "STATESTORE_DURABLE_LINEAGE_RESULTS_ONLY",
        )
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)


if __name__ == "__main__":
    unittest.main()
