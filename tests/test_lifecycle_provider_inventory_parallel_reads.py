from __future__ import annotations

import threading
import time

import pytest

from ues.lifecycle_runtime import _provider_inventory, _provider_inventory_read_workers


class FakeLifecycleClient:
    def __init__(self, *, fail_name: str | None = None) -> None:
        self.fail_name = fail_name
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def list_sources(self, *, page_size: int = 100):
        assert page_size == 100
        return [{"name": "sources/example", "repository": "hamad933/example"}]

    def list_sessions(self, *, page_size: int = 100):
        assert page_size == 100
        return [{"name": f"sessions/{index}"} for index in range(6)]

    def get_session(self, name: str):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.015 * (7 - int(name.rsplit("/", 1)[1])))
            if name == self.fail_name:
                raise RuntimeError("synthetic GET failure")
            return {"name": name, "sourceIdentifier": "sources/example", "state": "IN_PROGRESS"}
        finally:
            with self._lock:
                self.active -= 1

    def get_source(self, name: str):
        raise AssertionError(f"source {name} should have come from list_sources cache")


def test_provider_inventory_worker_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", raising=False)
    assert _provider_inventory_read_workers() == 8
    monkeypatch.setenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", "99")
    assert _provider_inventory_read_workers() == 8
    monkeypatch.setenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", "1")
    assert _provider_inventory_read_workers() == 1
    monkeypatch.setenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", "not-an-int")
    assert _provider_inventory_read_workers() == 8


def test_provider_inventory_hydrates_sessions_in_parallel_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", "4")
    client = FakeLifecycleClient()

    inventory = _provider_inventory(client)

    assert client.max_active >= 2
    assert [item["name"] for item in inventory] == [f"sessions/{index}" for index in range(6)]
    assert all(item["_source_repository"] == "hamad933/example" for item in inventory)


def test_provider_inventory_fails_closed_on_hydration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS", "4")
    client = FakeLifecycleClient(fail_name="sessions/3")

    with pytest.raises(RuntimeError, match="synthetic GET failure"):
        _provider_inventory(client)
