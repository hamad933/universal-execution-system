import unittest

from ues.recovery import reconcile_checkpoint


class RecoveryTests(unittest.TestCase):
    def test_matching_checkpoint_continues(self):
        result = reconcile_checkpoint(
            {"confirmed_head_sha": "a" * 40, "write_outcome": "CONFIRMED"},
            "a" * 40,
        )
        self.assertEqual(result["verdict"], "CHECKPOINT_MATCH")
        self.assertEqual(result["next_action"], "CONTINUE")

    def test_unknown_write_detects_expected_post_state(self):
        result = reconcile_checkpoint(
            {
                "confirmed_head_sha": "a" * 40,
                "expected_post_sha": "b" * 40,
                "write_outcome": "UNKNOWN",
            },
            "b" * 40,
        )
        self.assertEqual(result["verdict"], "WRITE_CONFIRMED_BY_POST_STATE")
        self.assertFalse(result["safe_to_blind_retry"])

    def test_unknown_write_never_blindly_retries_when_old_head_remains(self):
        result = reconcile_checkpoint(
            {
                "confirmed_head_sha": "a" * 40,
                "expected_post_sha": "b" * 40,
                "write_outcome": "UNKNOWN",
            },
            "a" * 40,
        )
        self.assertEqual(result["verdict"], "INTENDED_REMOTE_WRITE_NOT_OBSERVED")
        self.assertFalse(result["safe_to_blind_retry"])

    def test_moved_head_stops(self):
        result = reconcile_checkpoint(
            {"confirmed_head_sha": "a" * 40, "write_outcome": "CONFIRMED"},
            "c" * 40,
        )
        self.assertEqual(result["verdict"], "HEAD_MOVED")


if __name__ == "__main__":
    unittest.main()
