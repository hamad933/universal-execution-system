import unittest

from ues.operation_records import render_receipt_comment
from ues.write_recovery import recover_unobserved_format_operations


class WriteRecoveryTests(unittest.TestCase):
    def active_receipt(self, start_sha="a" * 40, state="UNKNOWN"):
        return {
            "schema_version": "0.6",
            "operation_id": "github-comment:1",
            "request_digest": "b" * 64,
            "repository": "owner/repo",
            "ref": "feature/x",
            "authority_event_id": "1",
            "start_sha": start_sha,
            "start_tree_sha": "c" * 40,
            "state": state,
            "safe_to_blind_retry": False,
            "extensions": {"operation": "format-fix"},
        }

    def comments(self, receipt):
        return [{"author": "github-actions[bot]", "body": render_receipt_comment(receipt)}]

    def test_unobserved_unknown_is_terminalized_when_head_is_unchanged(self):
        receipt = self.active_receipt()
        recovered = recover_unobserved_format_operations(
            self.comments(receipt),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["state"], "CANCELLED")
        self.assertEqual(
            recovered[0]["extensions"]["recovery"],
            "NO_REMOTE_WRITE_OBSERVED_AT_START_SHA",
        )

    def test_moved_head_is_never_auto_recovered(self):
        receipt = self.active_receipt()
        recovered = recover_unobserved_format_operations(
            self.comments(receipt),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="d" * 40,
        )
        self.assertEqual(recovered, [])

    def test_terminal_receipt_is_not_recovered(self):
        receipt = self.active_receipt(state="CONFIRMED")
        recovered = recover_unobserved_format_operations(
            self.comments(receipt),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
        )
        self.assertEqual(recovered, [])


if __name__ == "__main__":
    unittest.main()
