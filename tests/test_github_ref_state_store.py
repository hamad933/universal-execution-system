from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping

from ues.identity import canonical_lane_id
from ues.state_backends.github_refs import (
    GitHubGitDataTransport,
    GitHubRefConflict,
    GitHubRefStateStore,
    GitHubRefTransportError,
    GitHubRefWriteUncertain,
)
from ues.state_store import (
    LeaseCollision,
    OperationRecord,
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    production_state_store_assessment,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)


class MemoryGitRefTransport:
    repository = "owner/private-runtime-state"

    def __init__(self, *, private: bool = True):
        self.private = private
        self.refs: dict[str, str] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.parents: dict[str, str | None] = {}
        self.counter = 0
        self.create_mode = "normal"
        self.update_mode = "normal"
        self.read_mode = "normal"

    def assert_private_repository(self) -> None:
        if not self.private:
            raise GitHubRefTransportError("runtime state repository must be private")

    def get_ref(self, ref: str) -> str | None:
        if self.read_mode == "unavailable":
            raise GitHubRefTransportError("simulated read outage")
        return self.refs.get(ref)

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]:
        if commit_sha not in self.snapshots:
            raise GitHubRefTransportError("missing snapshot")
        return copy.deepcopy(self.snapshots[commit_sha])

    def create_snapshot_commit(
        self,
        *,
        parent_sha: str | None,
        snapshot: Mapping[str, Any],
        message: str,
    ) -> str:
        self.counter += 1
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        commit = sha256(f"{self.counter}|{parent_sha}|{message}|{raw}".encode()).hexdigest()
        self.snapshots[commit] = copy.deepcopy(dict(snapshot))
        self.parents[commit] = parent_sha
        return commit

    def create_ref(self, ref: str, commit_sha: str) -> None:
        mode, self.create_mode = self.create_mode, "normal"
        if ref in self.refs:
            raise GitHubRefConflict("exists")
        if mode == "conflict":
            raise GitHubRefConflict("simulated conflict")
        if mode == "uncertain_applied":
            self.refs[ref] = commit_sha
            raise GitHubRefWriteUncertain("simulated")
        if mode == "uncertain_not_applied":
            raise GitHubRefWriteUncertain("simulated")
        if mode == "uncertain_diverged":
            self.refs[ref] = "external-divergence"
            raise GitHubRefWriteUncertain("simulated")
        self.refs[ref] = commit_sha

    def update_ref(self, ref: str, commit_sha: str) -> None:
        mode, self.update_mode = self.update_mode, "normal"
        current = self.refs.get(ref)
        if current is None:
            raise GitHubRefConflict("missing")
        if self.parents.get(commit_sha) != current:
            raise GitHubRefConflict("non-fast-forward")
        if mode == "conflict":
            raise GitHubRefConflict("simulated conflict")
        if mode == "uncertain_applied":
            self.refs[ref] = commit_sha
            raise GitHubRefWriteUncertain("simulated")
        if mode == "uncertain_not_applied":
            raise GitHubRefWriteUncertain("simulated")
        if mode == "uncertain_diverged":
            self.refs[ref] = "external-divergence"
            raise GitHubRefWriteUncertain("simulated")
        self.refs[ref] = commit_sha


def runtime_record(lane_id: str, project: str = "GS", workstream: str = "W01"):
    return WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project=project,
        route=f"INTERNAL:{project}",
        workstream_id=workstream,
        actor_bindings={
            "WRITER": {
                "provider": "jules",
                "session_id": f"{project.lower()}-writer",
                "proof_status": "PROVEN_EXPLICIT",
                "source_repository": "owner/project",
            },
            "REVIEWER": {
                "provider": "jules",
                "session_id": f"{project.lower()}-reviewer",
                "proof_status": "PROVEN_EXPLICIT",
                "source_repository": "owner/project",
            },
        },
    )


class GitHubRefStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.transport = MemoryGitRefTransport()
        self.store = GitHubRefStateStore(self.transport, clock=lambda: T0)

    def test_capabilities_are_cross_run_ready(self):
        assessment = production_state_store_assessment(self.store)
        self.assertTrue(assessment["ready_for_cross_run_production"])
        self.assertEqual(assessment["missing_capabilities"], [])
        self.assertEqual(assessment["backend_name"], "github-private-ref-cas-v1")

    def test_public_state_repository_is_rejected_before_use(self):
        with self.assertRaises(GitHubRefTransportError):
            GitHubRefStateStore(MemoryGitRefTransport(private=False), clock=lambda: T0)

    def test_production_transport_repr_never_exposes_token(self):
        transport = GitHubGitDataTransport("owner/private-state", "super-secret-token")
        rendered = repr(transport)
        self.assertNotIn("super-secret-token", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_operation_read_outage_raises_instead_of_looking_missing(self):
        key = "ues-v2:waiting-answer:" + "c" * 64
        self.transport.read_mode = "unavailable"
        with self.assertRaises(StateUnavailable):
            self.store.read_operation(key)

    def test_same_workstream_in_two_projects_uses_distinct_lane_refs(self):
        gs = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        cep = canonical_lane_id("CEP", "PERSONAL:CEP", "W01")
        self.assertNotEqual(self.store.lane_ref(gs), self.store.lane_ref(cep))
        self.assertIn("/lane/", self.store.lane_ref(gs))
        self.assertNotIn("W01", self.store.lane_ref(gs))

    def test_lane_survives_runner_replacement_and_cas_conflict_fails_closed(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        first = self.store.compare_and_swap_workstream(lane, 0, runtime_record(lane))
        self.assertEqual(first.version, 1)

        runner_b = GitHubRefStateStore(self.transport, clock=lambda: T0)
        restored = runner_b.read_workstream(lane)
        self.assertEqual(restored.status, "OK")
        self.assertEqual(restored.record.actor_bindings["WRITER"]["session_id"], "gs-writer")

        stale_copy = runtime_record(lane)
        self.store.compare_and_swap_workstream(lane, 1, stale_copy)
        with self.assertRaises(StateVersionConflict):
            runner_b.compare_and_swap_workstream(lane, 1, stale_copy)
        self.assertEqual(runner_b.read_workstream(lane).version, 2)

    def test_operation_ref_is_durable_without_reversible_lane_lookup(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        key = "ues-v2:waiting-answer:" + "a" * 64
        op = OperationRecord(
            operation_key=key,
            lane_id=lane,
            workstream_id="W01",
            action="waiting-answer",
            request_digest="b" * 64,
            state="IN_FLIGHT",
            owner="runner-a",
            started_at="2026-08-24T00:00:00Z",
            updated_at="2026-08-24T00:00:00Z",
        )
        saved = self.store.compare_and_swap_operation(key, 0, op)
        self.assertEqual(saved.version, 1)
        self.assertNotEqual(self.store.operation_ref(key), self.store.lane_ref(lane))

        runner_b = GitHubRefStateStore(self.transport, clock=lambda: T0)
        restored = runner_b.read_operation(key)
        self.assertEqual(restored.status, "OK")
        self.assertEqual(restored.record.lane_id, lane)
        self.assertEqual(restored.record.state, "IN_FLIGHT")

    def test_nested_secret_shapes_are_redacted_before_persistence(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        record = runtime_record(lane)
        record.last_observed_provider_state = {
            "state": "IN_PROGRESS",
            "api_key": "raw-secret-value",
            "nested": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
        }
        self.store.compare_and_swap_workstream(lane, 0, record)
        raw = json.dumps(list(self.transport.snapshots.values()), sort_keys=True)
        self.assertNotIn("raw-secret-value", raw)
        self.assertNotIn("Bearer abcdefghijklmnopqrstuvwxyz", raw)
        restored = self.store.read_workstream(lane).record
        self.assertEqual(restored.last_observed_provider_state["api_key"], "[REDACTED]")

    def test_non_force_update_conflict_never_overwrites(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        record = runtime_record(lane)
        self.store.compare_and_swap_workstream(lane, 0, record)
        before = self.transport.get_ref(self.store.lane_ref(lane))
        self.transport.update_mode = "conflict"
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(lane, 1, record)
        self.assertEqual(self.transport.get_ref(self.store.lane_ref(lane)), before)

    def test_ambiguous_update_applied_is_confirmed_only_by_ref_readback(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        record = runtime_record(lane)
        self.store.compare_and_swap_workstream(lane, 0, record)
        self.transport.update_mode = "uncertain_applied"
        saved = self.store.compare_and_swap_workstream(lane, 1, record)
        self.assertEqual(saved.version, 2)

    def test_ambiguous_update_not_observed_fails_closed_without_retry(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        record = runtime_record(lane)
        self.store.compare_and_swap_workstream(lane, 0, record)
        before = self.transport.get_ref(self.store.lane_ref(lane))
        self.transport.update_mode = "uncertain_not_applied"
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(lane, 1, record)
        self.assertEqual(self.transport.get_ref(self.store.lane_ref(lane)), before)
        self.assertEqual(self.store.read_workstream(lane).version, 1)

    def test_ambiguous_divergence_requires_reconciliation(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        record = runtime_record(lane)
        self.store.compare_and_swap_workstream(lane, 0, record)
        self.transport.update_mode = "uncertain_diverged"
        with self.assertRaises(StateUnavailable):
            self.store.compare_and_swap_workstream(lane, 1, record)

    def test_initialization_race_fails_closed(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        self.transport.create_mode = "uncertain_not_applied"
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(lane, 0, runtime_record(lane))
        self.assertEqual(self.store.read_workstream(lane).status, "MISSING")

    def test_lane_local_lease_is_durable_and_competing_runner_is_blocked(self):
        lane = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        self.store.compare_and_swap_workstream(lane, 0, runtime_record(lane))
        acquired = self.store.acquire_lease(
            lane, "runner-a", "operation-a", 60, now=T0
        )
        runner_b = GitHubRefStateStore(self.transport, clock=lambda: T0)
        with self.assertRaises(LeaseCollision) as raised:
            runner_b.acquire_lease(lane, "runner-b", "operation-b", 60, now=T0)
        self.assertIn("leased", str(raised.exception))
        released = runner_b.release_lease(lane, acquired.lease.lease_id, now=T0)
        self.assertIsNone(released.record.lease)

    def test_unrelated_lanes_do_not_contend_on_one_global_ref(self):
        gs = canonical_lane_id("GS", "INTERNAL:GS", "W01")
        cep = canonical_lane_id("CEP", "PERSONAL:CEP", "W01")
        self.store.compare_and_swap_workstream(gs, 0, runtime_record(gs, "GS"))
        self.store.compare_and_swap_workstream(cep, 0, runtime_record(cep, "CEP"))
        self.assertEqual(self.store.read_workstream(gs).version, 1)
        self.assertEqual(self.store.read_workstream(cep).version, 1)
        self.assertNotEqual(self.store.lane_ref(gs), self.store.lane_ref(cep))


if __name__ == "__main__":
    unittest.main()
