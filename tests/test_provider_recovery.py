import unittest

from ues.failures import classify_provider_failure
from ues.recovery import reconcile_provider_write


class ProviderRecoveryTests(unittest.TestCase):
    def test_provider_failure_classification(self):
        cases = [
            ({"status_code": 401}, "PROVIDER_AUTHENTICATION"),
            ({"status_code": 403}, "PROVIDER_AUTHORIZATION"),
            ({"status_code": 404}, "PROVIDER_NOT_FOUND"),
            ({"status_code": 429, "retry_after": 4}, "PROVIDER_RATE_LIMIT"),
            ({"status_code": 503}, "PROVIDER_SERVER_ERROR"),
            ({"network_error": True}, "PROVIDER_NETWORK_ERROR"),
            ({"protocol_error": True}, "PROVIDER_PROTOCOL_ERROR"),
            ({"write_outcome_unknown": True}, "WRITE_OUTCOME_UNKNOWN"),
        ]
        for input_data, expected in cases:
            with self.subTest(expected=expected):
                result = classify_provider_failure(input_data)
                self.assertEqual(result["category"], expected)
                self.assertFalse(result["safe_to_blind_retry"])

    def test_recovery_requires_authoritative_read(self):
        result = reconcile_provider_write(
            {
                "write_outcome": "WRITE_OUTCOME_UNKNOWN",
                "authoritative_read_complete": False,
                "post_session_state": "IN_PROGRESS",
            }
        )
        self.assertEqual(result["verdict"], "AUTHORITATIVE_READ_INCOMPLETE")
        self.assertFalse(result["safe_to_blind_retry"])

    def test_unknown_post_state_fails_closed(self):
        result = reconcile_provider_write(
            {
                "write_outcome": "WRITE_OUTCOME_UNKNOWN",
                "authoritative_read_complete": True,
                "post_session_state": "UNKNOWN",
                "post_activities": [],
            }
        )
        self.assertEqual(result["verdict"], "UNKNOWN_PROVIDER_STATE")
        self.assertFalse(result["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
