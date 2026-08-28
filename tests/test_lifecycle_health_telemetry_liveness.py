from __future__ import annotations

import unittest
from types import SimpleNamespace

from ues.lifecycle_runtime_observed import _persist_health_with_runtime_binding
from ues.state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


class FakeStore:
    def __init__(self, *, read_status: str = "OK", cas_conflict: bool = False):
        self.read_status = read_status
        self.cas_conflict = cas_conflict
        self.record = WorkstreamRuntimeRecord(
            lane_id="lane",
            project="RP03",
            route="RP03",
            workstream_id="LIFECYCLE-RUNTIME-HEALTH",
            activation_mode="SHADOW",
        )

    def read_workstream(self, lane_id: str):
        if self.read_status != "OK":
            return SimpleNamespace(status=self.read_status, record=None, version=0, reason="telemetry read unavailable")
        return SimpleNamespace(status="OK", record=self.record, version=7, reason=None)

    def compare_and_swap_workstream(self, lane_id: str, expected_version: int, record):
        if self.cas_conflict:
            raise StateVersionConflict("telemetry CAS conflict")
        self.record = record
        return SimpleNamespace(status="OK", version=8, reason=None)


class LifecycleHealthTelemetryLivenessTests(unittest.TestCase):
    BINDING = {
        "status": "BOUND",
        "sha": "a" * 40,
        "telemetry_grants_no_authority": True,
    }

    def test_initial_health_state_conflict_is_degraded_not_global_stop(self):
        def conflicting_original(*args, **kwargs):
            raise StateVersionConflict("uncertain telemetry write")

        persist = _persist_health_with_runtime_binding(conflicting_original, self.BINDING)
        result = persist(
            FakeStore(),
            project="RP03",
            route="RP03",
            status="IN_FLIGHT",
            summary={"phase": "START"},
        )
        self.assertFalse(result["health_telemetry_durable"])
        self.assertFalse(result["runtime_binding_durable"])
        self.assertFalse(result["telemetry_failure_blocks_downstream_effects"])
        self.assertTrue(result["downstream_authority_and_state_gates_required"])
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(result["telemetry_error_category"], "StateVersionConflict")

    def test_runtime_binding_read_unavailable_after_health_write_is_degraded(self):
        def successful_original(*args, **kwargs):
            return {"lane_id": "lane", "version": 7, "status": "IN_FLIGHT"}

        persist = _persist_health_with_runtime_binding(successful_original, self.BINDING)
        result = persist(
            FakeStore(read_status="UNAVAILABLE"),
            project="RP03",
            route="RP03",
            status="IN_FLIGHT",
            summary={"phase": "START"},
        )
        self.assertTrue(result["health_telemetry_durable"])
        self.assertFalse(result["runtime_binding_durable"])
        self.assertFalse(result["telemetry_failure_blocks_downstream_effects"])
        self.assertEqual(result["telemetry_error_category"], "StateUnavailable")

    def test_runtime_binding_cas_conflict_after_health_write_is_degraded(self):
        def successful_original(*args, **kwargs):
            return {"lane_id": "lane", "version": 7, "status": "PASS"}

        persist = _persist_health_with_runtime_binding(successful_original, self.BINDING)
        result = persist(
            FakeStore(cas_conflict=True),
            project="RP03",
            route="RP03",
            status="PASS",
            summary={"phase": "END"},
        )
        self.assertTrue(result["health_telemetry_durable"])
        self.assertFalse(result["runtime_binding_durable"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_non_telemetry_programming_error_is_not_swallowed(self):
        def bad_original(*args, **kwargs):
            raise ValueError("bad lifecycle contract")

        persist = _persist_health_with_runtime_binding(bad_original, self.BINDING)
        with self.assertRaises(ValueError):
            persist(
                FakeStore(),
                project="RP03",
                route="RP03",
                status="IN_FLIGHT",
                summary={"phase": "START"},
            )


if __name__ == "__main__":
    unittest.main()
