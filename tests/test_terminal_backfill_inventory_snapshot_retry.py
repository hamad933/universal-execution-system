from __future__ import annotations

import pytest

from ues.providers.base import NetworkError, ProtocolError, ServerError
from ues.terminal_backfill import _InventorySnapshotRetryClient


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


def test_transient_session_inventory_failure_recovers_without_rereading_sources() -> None:
    delegate = _FakeJules()
    delegate.session_failures = [NetworkError("transient", operation="jules.sessions.list")]
    client = _InventorySnapshotRetryClient(delegate, attempts=2)

    assert client.list_sources() == [{"name": "sources/1"}]
    assert client.list_sessions() == [{"name": "sessions/1"}]

    assert delegate.source_calls == 1
    assert delegate.session_calls == 2
    assert client.source_inventory_attempts == 1
    assert client.session_inventory_attempts == 2


def test_persistent_transient_inventory_failure_stops_at_bound() -> None:
    delegate = _FakeJules()
    delegate.source_failures = [
        NetworkError("first", operation="jules.sources.list"),
        ServerError("second", operation="jules.sources.list"),
        NetworkError("must not be consumed", operation="jules.sources.list"),
    ]
    client = _InventorySnapshotRetryClient(delegate, attempts=2)

    with pytest.raises(ServerError):
        client.list_sources()

    assert delegate.source_calls == 2
    assert client.source_inventory_attempts == 2


def test_non_transient_inventory_error_is_not_outer_retried() -> None:
    delegate = _FakeJules()
    delegate.session_failures = [ProtocolError("bad shape", operation="jules.sessions.list")]
    client = _InventorySnapshotRetryClient(delegate, attempts=3)

    with pytest.raises(ProtocolError):
        client.list_sessions()

    assert delegate.session_calls == 1
    assert client.session_inventory_attempts == 1


def test_activity_reads_are_never_outer_retried() -> None:
    delegate = _FakeJules()
    client = _InventorySnapshotRetryClient(delegate, attempts=3)

    assert client.list_activities("sessions/1") == [{"name": "activities/1"}]
    assert delegate.activity_calls == 1
