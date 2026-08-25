from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues import terminal_recovery as recovery
from ues.terminal_recovery_runtime import (
    _CachingReadOnlyClient,
    _activity_workers,
    _prefetch_exact_bound_activities,
)


class FakeStore:
    def read_workstream(self, lane_id):
        return SimpleNamespace(status="MISSING", record=None)


class FakeClient:
    def __init__(self, fail_session: str | None = None):
        self.fail_session = fail_session
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    def list_activities(self, session, *, page_size=100):
        with self.lock:
            self.calls.append(session)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            if session == self.fail_session:
                raise RuntimeError("private provider failure text")
            return [{"agentMessaged": {"agentMessage": "safe"}}]
        finally:
            with self.lock:
                self.active -= 1


class TerminalRecoveryParallelReadTests(unittest.TestCase):
    def setUp(self):
        self.projects = ({"project": "RP04", "route": "RP04", "repository": "owner/repo"},)
        self.sources = [
            {"name": "sources/rp04", "repository": "owner/repo", "explicitRepositoryIdentity": True}
        ]
        self.sessions = [
            {
                "name": f"sessions/rp04-{index}",
                "normalizedState": "COMPLETED",
                "sourceIdentifier": "sources/rp04",
            }
            for index in range(4)
        ]
        self.indexes = {
            "RP04": {
                recovery.session_fingerprint(f"sessions/rp04-{index}"): [
                    {"lane_id": f"lane-{index}", "workstream": f"S{index}", "role": "REVIEWER", "generation": 1}
                ]
                for index in range(4)
            }
        }

    def test_exact_bound_reads_overlap(self):
        client = FakeClient()
        with patch.dict("os.environ", {"UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS": "4"}, clear=False):
            cache, workers = _prefetch_exact_bound_activities(
                client,
                store=FakeStore(),
                projects=self.projects,
                indexes=self.indexes,
                sources=self.sources,
                sessions=self.sessions,
            )
        self.assertEqual(workers, 4)
        self.assertEqual(len(cache), 4)
        self.assertGreaterEqual(client.max_active, 2)
        self.assertEqual(set(cache), {f"sessions/rp04-{index}" for index in range(4)})
        self.assertTrue(all(ok for ok, _ in cache.values()))

    def test_unbound_completed_session_is_not_prefetched(self):
        indexes = {"RP04": dict(self.indexes["RP04"])}
        indexes["RP04"].pop(recovery.session_fingerprint("sessions/rp04-3"))
        client = FakeClient()
        cache, _ = _prefetch_exact_bound_activities(
            client,
            store=FakeStore(),
            projects=self.projects,
            indexes=indexes,
            sources=self.sources,
            sessions=self.sessions,
        )
        self.assertEqual(len(cache), 3)
        self.assertNotIn("sessions/rp04-3", cache)
        self.assertNotIn("sessions/rp04-3", client.calls)

    def test_cached_failure_is_replayed_without_persisting_text(self):
        failed = "sessions/rp04-2"
        client = FakeClient(fail_session=failed)
        cache, _ = _prefetch_exact_bound_activities(
            client,
            store=FakeStore(),
            projects=self.projects,
            indexes=self.indexes,
            sources=self.sources,
            sessions=self.sessions,
        )
        wrapped = _CachingReadOnlyClient(client, sources=self.sources, sessions=self.sessions, activities=cache)
        with self.assertRaisesRegex(RuntimeError, "private provider failure text"):
            wrapped.list_activities(failed)
        self.assertEqual(sum(1 for ok, _ in cache.values() if ok), 3)

    def test_worker_override_is_bounded(self):
        with patch.dict("os.environ", {"UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS": "99"}, clear=False):
            self.assertEqual(_activity_workers(), 8)
        with patch.dict("os.environ", {"UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS": "0"}, clear=False):
            self.assertEqual(_activity_workers(), 1)
        with patch.dict("os.environ", {"UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS": "invalid"}, clear=False):
            self.assertEqual(_activity_workers(), 4)


if __name__ == "__main__":
    unittest.main()
