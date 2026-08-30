from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.initial_lineage_runtime import (
    _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS,
    _provider_inventory_snapshot_attempts,
    _provider_inventory_with_retry,
)
from ues.providers.base import NetworkError, RateLimitError


class InitialLineageProviderInventoryRetryTests(unittest.TestCase):
    def test_retry_recovers_after_first_allowlisted_transient_read_failure(self) -> None:
        outage = RateLimitError("provider rate limited", operation="jules.sessions.list")
        inventory = [{"name": "sessions/example"}]
        with patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory",
            side_effect=[outage, inventory],
        ) as provider_inventory:
            result, attempts, attempt_limit = _provider_inventory_with_retry(object())

        self.assertEqual(result, inventory)
        self.assertEqual(attempts, 2)
        self.assertEqual(attempt_limit, 2)
        self.assertEqual(provider_inventory.call_count, 2)

    def test_exhausted_allowlisted_retry_remains_bounded_and_raises_final_read_error(self) -> None:
        outage = NetworkError("provider network request failed", operation="jules.sessions.get")
        with patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory",
            side_effect=[outage, outage, outage, outage],
        ) as provider_inventory:
            with self.assertRaises(NetworkError):
                _provider_inventory_with_retry(object(), attempt_limit=3)

        self.assertEqual(provider_inventory.call_count, 3)

    def test_configured_attempt_count_is_hard_capped(self) -> None:
        with patch.dict(
            os.environ,
            {"UES_INITIAL_LINEAGE_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS": "99"},
            clear=False,
        ):
            self.assertEqual(
                _provider_inventory_snapshot_attempts(),
                _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS,
            )
        self.assertEqual(_MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS, 3)

    def test_non_allowlisted_or_possible_post_write_operation_is_never_retried(self) -> None:
        outage = NetworkError(
            "provider network request failed",
            operation="jules.sessions.sendMessage",
        )
        with patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory",
            side_effect=outage,
        ) as provider_inventory:
            with self.assertRaises(NetworkError):
                _provider_inventory_with_retry(object(), attempt_limit=3)

        self.assertEqual(provider_inventory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
