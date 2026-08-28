from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.terminal_backfill import _backfill_provider_client, _backfill_provider_policy


class TerminalBackfillProviderPolicyTests(unittest.TestCase):
    def test_default_policy_is_shorter_than_general_jules_reads(self):
        with patch.dict(os.environ, {}, clear=True):
            timeout, attempts = _backfill_provider_policy()
        self.assertEqual(timeout, 5.0)
        self.assertEqual(attempts, 2)

    def test_policy_overrides_are_hard_bounded(self):
        with patch.dict(
            os.environ,
            {
                "UES_TERMINAL_BACKFILL_PROVIDER_TIMEOUT_SECONDS": "999",
                "UES_TERMINAL_BACKFILL_PROVIDER_READ_ATTEMPTS": "99",
            },
            clear=True,
        ):
            timeout, attempts = _backfill_provider_policy()
        self.assertEqual(timeout, 10.0)
        self.assertEqual(attempts, 2)

        with patch.dict(
            os.environ,
            {
                "UES_TERMINAL_BACKFILL_PROVIDER_TIMEOUT_SECONDS": "0",
                "UES_TERMINAL_BACKFILL_PROVIDER_READ_ATTEMPTS": "0",
            },
            clear=True,
        ):
            timeout, attempts = _backfill_provider_policy()
        self.assertEqual(timeout, 1.0)
        self.assertEqual(attempts, 1)

    def test_invalid_policy_falls_back_to_safe_defaults(self):
        with patch.dict(
            os.environ,
            {
                "UES_TERMINAL_BACKFILL_PROVIDER_TIMEOUT_SECONDS": "bad",
                "UES_TERMINAL_BACKFILL_PROVIDER_READ_ATTEMPTS": "bad",
            },
            clear=True,
        ):
            timeout, attempts = _backfill_provider_policy()
        self.assertEqual(timeout, 5.0)
        self.assertEqual(attempts, 2)

    def test_client_uses_bounded_read_policy_and_no_key_stays_unconstructed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_backfill_provider_client())

        with patch.dict(
            os.environ,
            {
                "JULES_API_KEY": "test-key",
                "UES_TERMINAL_BACKFILL_PROVIDER_TIMEOUT_SECONDS": "4",
                "UES_TERMINAL_BACKFILL_PROVIDER_READ_ATTEMPTS": "1",
            },
            clear=True,
        ):
            client = _backfill_provider_client()
        self.assertIsNotNone(client)
        self.assertEqual(client._timeout, 4.0)
        self.assertEqual(client._read_retry_policy.max_attempts, 1)
        self.assertEqual(client._read_retry_policy.max_retry_after_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
