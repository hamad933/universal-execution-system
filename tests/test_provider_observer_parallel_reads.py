from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from ues.provider_observer_runtime import (
    _activity_read_workers,
    _fingerprint,
    collect_resilient_observation,
)


class ActivityFailure(Exception):
    category = "ACTIVITY_READ_FAILED"


class ParallelClient:
    def __init__(self, *, fail_session: str | None = None):
        self.fail_session = fail_session
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.activity_calls: list[str] = []

    def list_sources(self, *, page_size=100):
        return [
            {
                "name": "sources/gs",
                "repository": "hamad933/GS-2",
                "explicitRepositoryIdentity": True,
            }
        ]

    def list_sessions(self, *, page_size=100):
        return [
            {
                "name": f"sessions/gs-complete-{index}",
                "title": f"GS-G70-JULES-IPA-{index}",
                "normalizedState": "COMPLETED",
                "stateAuthoritative": True,
                "sourceIdentifier": "sources/gs",
            }
            for index in range(4)
        ]

    def list_activities(self, session, *, page_size=100):
        with self.lock:
            self.activity_calls.append(session)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            if session == self.fail_session:
                raise ActivityFailure("private provider text")
            return [
                {
                    "name": f"activities/{session.rsplit('/', 1)[-1]}",
                    "type": "AGENT_MESSAGE",
                    "createTime": "2026-08-25T00:00:00Z",
                    "agentMessaged": {"agentMessage": "review complete"},
                }
            ]
        finally:
            with self.lock:
                self.active -= 1


class ParallelActivityReadTests(unittest.TestCase):
    def _bound(self):
        return {
            "GS": {
                _fingerprint(f"sessions/gs-complete-{index}")
                for index in range(4)
            }
        }

    def test_exact_bound_terminal_reads_overlap_and_persist_deterministically(self):
        client = ParallelClient()
        with patch.dict("os.environ", {"UES_PROVIDER_ACTIVITY_READ_WORKERS": "4"}, clear=False):
            result = collect_resilient_observation(
                client,
                observed_at="2026-08-25T00:10:00Z",
                bound_terminal_fingerprints=self._bound(),
            )

        self.assertGreaterEqual(client.max_active, 2)
        self.assertEqual(result["activity_read_workers"], 4)
        sessions = result["projects"]["GS"]["sessions"]
        self.assertEqual(
            [item["session_fingerprint"] for item in sessions],
            sorted(item["session_fingerprint"] for item in sessions),
        )
        self.assertTrue(all(item["activity_read_complete"] for item in sessions))
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["activity_content_persisted"])

    def test_one_activity_failure_does_not_cancel_peer_reads(self):
        failed = "sessions/gs-complete-2"
        client = ParallelClient(fail_session=failed)
        with (
            patch.dict("os.environ", {"UES_PROVIDER_ACTIVITY_READ_WORKERS": "4"}, clear=False),
            patch("ues.provider_observer_runtime._READ_ERRORS", (ActivityFailure,)),
        ):
            result = collect_resilient_observation(
                client,
                observed_at="2026-08-25T00:10:00Z",
                bound_terminal_fingerprints=self._bound(),
            )

        sessions = result["projects"]["GS"]["sessions"]
        failed_fp = _fingerprint(failed)
        failed_result = next(item for item in sessions if item["session_fingerprint"] == failed_fp)
        self.assertFalse(failed_result["activity_read_complete"])
        self.assertEqual(failed_result["activity_read_error_category"], "ACTIVITY_READ_FAILED")
        self.assertEqual(failed_result["_terminal_candidate"]["state"], "COMPLETED_OUTPUT_UNCONSUMED")
        self.assertEqual(sum(bool(item["activity_read_complete"]) for item in sessions), 3)
        self.assertNotIn("private provider text", str(result))

    def test_worker_override_is_bounded(self):
        with patch.dict("os.environ", {"UES_PROVIDER_ACTIVITY_READ_WORKERS": "99"}, clear=False):
            self.assertEqual(_activity_read_workers(), 8)
        with patch.dict("os.environ", {"UES_PROVIDER_ACTIVITY_READ_WORKERS": "0"}, clear=False):
            self.assertEqual(_activity_read_workers(), 1)
        with patch.dict("os.environ", {"UES_PROVIDER_ACTIVITY_READ_WORKERS": "invalid"}, clear=False):
            self.assertEqual(_activity_read_workers(), 4)


if __name__ == "__main__":
    unittest.main()
