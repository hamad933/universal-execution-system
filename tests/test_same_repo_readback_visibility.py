from __future__ import annotations

import copy
import json
import unittest
from hashlib import sha256
from typing import Any, Mapping

from ues.identity import canonical_lane_id
from ues.state_backends.github_refs import GitHubRefConflict, GitHubRefTransportError
from ues.state_backends.public_same_repo import OwnerAuthorizedSameRepoStateStore
from ues.state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


class DelayedVisibilityTransport:
    repository = "hamad933/universal-execution-system"

    def __init__(self) -> None:
        self.refs: dict[str, str] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.parents: dict[str, str | None] = {}
        self.counter = 0
        self.update_calls = 0
        self.update_mode = "normal"
        self.stale_reads_remaining = 0
        self.stale_value: str | None = None
        self.post_update_read_errors = 0
        self.read_errors_remaining = 0

    def assert_private_repository(self) -> None:
        return None

    def get_ref(self, ref: str) -> str | None:
        if self.read_errors_remaining > 0:
            self.read_errors_remaining -= 1
            raise GitHubRefTransportError("GitHub API request failed (HTTP 403)")
        if self.stale_reads_remaining > 0:
            self.stale_reads_remaining -= 1
            return self.stale_value
        return self.refs.get(ref)

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]:
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
        if ref in self.refs:
            raise GitHubRefConflict("exists")
        self.refs[ref] = commit_sha

    def update_ref(self, ref: str, commit_sha: str) -> None:
        self.update_calls += 1
        current = self.refs.get(ref)
        if current is None or self.parents.get(commit_sha) != current:
            raise GitHubRefConflict("non-fast-forward")
        if self.update_mode == "delayed_visible":
            self.refs[ref] = commit_sha
            self.stale_value = current
            self.stale_reads_remaining = 2
        elif self.update_mode == "not_applied":
            self.stale_value = current
            self.stale_reads_remaining = 10
        else:
            self.refs[ref] = commit_sha
        self.read_errors_remaining = self.post_update_read_errors


class FinalReadOutageStateStore(OwnerAuthorizedSameRepoStateStore):
    def __init__(self, *args, **kwargs) -> None:
        self.final_lane_read_failures = 0
        super().__init__(*args, **kwargs)

    def read_workstream(self, lane_id: str):
        if self.final_lane_read_failures > 0:
            self.final_lane_read_failures -= 1
            return self._shadow("UNAVAILABLE", "transient final post-CAS read unavailable")
        return super().read_workstream(lane_id)


def runtime_record(lane_id: str) -> WorkstreamRuntimeRecord:
    return WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project="GS",
        route="GS",
        workstream_id="LIFECYCLE-RUNTIME-HEALTH",
        activation_mode="SHADOW",
    )


class SameRepoReadbackVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = DelayedVisibilityTransport()
        self.store = FinalReadOutageStateStore(
            self.transport,
            ref_prefix="ues-runtime/v2",
        )
        self.store.publish_readback_attempts = 4
        self.store.publish_readback_delay_seconds = 0.0
        self.store.post_cas_readback_attempts = 3
        self.store.post_cas_readback_delay_seconds = 0.0
        self.lane = canonical_lane_id("GS", "GS", "LIFECYCLE-RUNTIME-HEALTH")
        self.store.compare_and_swap_workstream(self.lane, 0, runtime_record(self.lane))

    def test_delayed_ref_visibility_confirms_the_same_write_without_retrying_it(self) -> None:
        self.transport.update_mode = "delayed_visible"
        saved = self.store.compare_and_swap_workstream(
            self.lane,
            1,
            runtime_record(self.lane),
        )
        self.assertEqual(saved.version, 2)
        self.assertEqual(self.transport.update_calls, 1)
        self.assertEqual(self.store.read_workstream(self.lane).version, 2)

    def test_transient_post_write_readback_error_recovers_without_retrying_write(self) -> None:
        self.transport.post_update_read_errors = 1
        saved = self.store.compare_and_swap_workstream(
            self.lane,
            1,
            runtime_record(self.lane),
        )
        self.assertEqual(saved.version, 2)
        self.assertEqual(self.transport.update_calls, 1)
        self.assertEqual(self.store.read_workstream(self.lane).version, 2)

    def test_persistent_post_write_readback_error_fails_closed_without_retrying_write(self) -> None:
        self.transport.post_update_read_errors = self.store.publish_readback_attempts
        with self.assertRaises(StateUnavailable):
            self.store.compare_and_swap_workstream(
                self.lane,
                1,
                runtime_record(self.lane),
            )
        self.assertEqual(self.transport.update_calls, 1)
        self.transport.read_errors_remaining = 0
        self.assertEqual(self.store.read_workstream(self.lane).version, 2)

    def test_transient_final_post_cas_read_recovers_without_repeating_cas(self) -> None:
        self.store.final_lane_read_failures = 1
        saved = self.store.compare_and_swap_workstream(
            self.lane,
            1,
            runtime_record(self.lane),
        )
        self.assertEqual(saved.version, 2)
        self.assertEqual(self.transport.update_calls, 1)
        self.assertEqual(self.store.final_lane_read_failures, 0)

    def test_persistent_final_post_cas_read_fails_closed_without_repeating_cas(self) -> None:
        self.store.final_lane_read_failures = self.store.post_cas_readback_attempts
        with self.assertRaises(StateUnavailable):
            self.store.compare_and_swap_workstream(
                self.lane,
                1,
                runtime_record(self.lane),
            )
        self.assertEqual(self.transport.update_calls, 1)
        self.store.final_lane_read_failures = 0
        self.assertEqual(self.store.read_workstream(self.lane).version, 2)

    def test_unobserved_write_still_fails_closed_without_blind_retry(self) -> None:
        self.transport.update_mode = "not_applied"
        with self.assertRaises(StateVersionConflict):
            self.store.compare_and_swap_workstream(
                self.lane,
                1,
                runtime_record(self.lane),
            )
        self.assertEqual(self.transport.update_calls, 1)
        self.transport.stale_reads_remaining = 0
        self.assertEqual(self.store.read_workstream(self.lane).version, 1)


if __name__ == "__main__":
    unittest.main()
