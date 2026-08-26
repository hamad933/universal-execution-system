from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.providers.base import NetworkError, ProtocolError, ServerError
from ues.terminal_backfill import (
    _InventorySnapshotRetryClient,
    _backfill_provider_policy,
    _inventory_provider_policy,
)


class _FakeJules:
    def __init__(self) -> None:
        self.source_calls = 0
        self.session_calls = 0
        self.activity_calls = 0
        self.source_failures: list[BaseException] = []
        self.session_failures: list[BaseException] = []

    def list_sources(self, *, page_size: int = 100):
        self.source_calls += 1
        if self.source_failures:
            raise self.source_failures.pop(0)
        return [{"name": "sources/1"}]

    def list_sessions(self, *, page_size: int = 100):
        self.session_calls += 1
        if self.session_failures:
            raise self.session_failures.pop(0)
        return [{"name": "sessions/1"}]

    def list_activities(self, session: str, *, page_size: int = 100):
        self.activity_calls += 1
        return [{"name": "activities/1"}]


class TerminalBackfillInventorySnapshotRetryTests(unittest.TestCase):
    def test_transient_session_inventory_failure_recovers_without_rereading_sources(self) -> None:
        delegate = _FakeJules()
        delegate.session_failures = [NetworkError("transient", operation="jules.sessions.list")]
        client = _InventorySnapshotRetryClient(delegate, attempts=2)

        self.assertEqual(client.list_sources(), [{"name": "sources/1"}])
        self.assertEqual(client.list_sessions(), [{"name": "sessions/1"}])
        self.assertEqual(delegate.source_calls, 1)
        self.assertEqual(delegate.session_calls, 2)
        self.assertEqual(client.source_inventory_attempts, 1)
        self.assertEqual(client.session_inventory_attempts, 2)

    def test_persistent_transient_inventory_failure_stops_at_bound(self) -> None:
        delegate = _FakeJules()
        delegate.source_failures = [
            NetworkError("first", operation="jules.sources.list"),
            ServerError("second", operation="jules.sources.list"),
            NetworkError("must not be consumed", operation="jules.sources.list"),
        ]
        client = _InventorySnapshotRetryClient(delegate, attempts=2)

        with self.assertRaises(ServerError):
            client.list_sources()

        self.assertEqual(delegate.source_calls, 2)
        self.assertEqual(client.source_inventory_attempts, 2)

    def test_non_transient_inventory_error_is_not_outer_retried(self) -> None:
        delegate = _FakeJules()
        delegate.session_failures = [ProtocolError("bad shape", operation="jules.sessions.list")]
        client = _InventorySnapshotRetryClient(delegate, attempts=3)

        with self.assertRaises(ProtocolError):
            client.list_sessions()

        self.assertEqual(delegate.session_calls, 1)
        self.assertEqual(client.session_inventory_attempts, 1)

    def test_activity_reads_use_separate_delegate_and_are_never_outer_retried(self) -> None:
        inventory = _FakeJules()
        activity = _FakeJules()
        client = _InventorySnapshotRetryClient(inventory, attempts=3, activity_delegate=activity)

        self.assertEqual(client.list_sources(), [{"name": "sources/1"}])
        self.assertEqual(client.list_sessions(), [{"name": "sessions/1"}])
        self.assertEqual(client.list_activities("sessions/1"), [{"name": "activities/1"}])
        self.assertEqual(inventory.source_calls, 1)
        self.assertEqual(inventory.session_calls, 1)
        self.assertEqual(inventory.activity_calls, 0)
        self.assertEqual(activity.source_calls, 0)
        self.assertEqual(activity.session_calls, 0)
        self.assertEqual(activity.activity_calls, 1)

    def test_inventory_default_budget_is_longer_than_activity_budget(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            activity_timeout, activity_attempts = _backfill_provider_policy()
            inventory_timeout, inventory_attempts = _inventory_provider_policy()
        self.assertGreater(inventory_timeout, activity_timeout)
        self.assertGreaterEqual(inventory_attempts, activity_attempts)

    def test_inventory_policy_remains_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {
                "UES_TERMINAL_BACKFILL_INVENTORY_PROVIDER_TIMEOUT_SECONDS": "999",
                "UES_TERMINAL_BACKFILL_INVENTORY_PROVIDER_READ_ATTEMPTS": "999",
            },
            clear=False,
        ):
            timeout, attempts = _inventory_provider_policy()
        self.assertEqual(timeout, 30.0)
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()
