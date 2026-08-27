from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from ues.lifecycle_runtime import _provider_inventory, _provider_inventory_read_workers


class FakeLifecycleClient:
    def __init__(self, *, fail_name: str | None = None) -> None:
        self.fail_name = fail_name
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def list_sources(self, *, page_size: int = 100):
        self.assert_page_size(page_size)
        return [{"name": "sources/example", "repository": "hamad933/example"}]

    def list_sessions(self, *, page_size: int = 100):
        self.assert_page_size(page_size)
        return [{"name": f"sessions/{index}"} for index in range(6)]

    @staticmethod
    def assert_page_size(page_size: int) -> None:
        if page_size != 100:
            raise AssertionError(f"unexpected page_size={page_size}")

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


class ProviderInventoryParallelReadTests(unittest.TestCase):
    def test_provider_inventory_worker_count_is_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_provider_inventory_read_workers(), 8)
        with patch.dict(os.environ, {"UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS": "99"}, clear=False):
            self.assertEqual(_provider_inventory_read_workers(), 8)
        with patch.dict(os.environ, {"UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS": "1"}, clear=False):
            self.assertEqual(_provider_inventory_read_workers(), 1)
        with patch.dict(os.environ, {"UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS": "not-an-int"}, clear=False):
            self.assertEqual(_provider_inventory_read_workers(), 8)

    def test_provider_inventory_hydrates_sessions_in_parallel_and_preserves_order(self) -> None:
        client = FakeLifecycleClient()
        with patch.dict(os.environ, {"UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS": "4"}, clear=False):
            inventory = _provider_inventory(client)

        self.assertGreaterEqual(client.max_active, 2)
        self.assertEqual([item["name"] for item in inventory], [f"sessions/{index}" for index in range(6)])
        self.assertTrue(all(item["_source_repository"] == "hamad933/example" for item in inventory))

    def test_provider_inventory_fails_closed_on_hydration_error(self) -> None:
        client = FakeLifecycleClient(fail_name="sessions/3")
        with patch.dict(os.environ, {"UES_LIFECYCLE_PROVIDER_INVENTORY_READ_WORKERS": "4"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "synthetic GET failure"):
                _provider_inventory(client)


if __name__ == "__main__":
    unittest.main()
