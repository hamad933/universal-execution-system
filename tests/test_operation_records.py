import unittest

from ues.operation_records import render_receipt_comment, trusted_operation_records


class OperationRecordTests(unittest.TestCase):
    def receipt(self):
        return {
            "schema_version": "0.6",
            "operation_id": "github-comment:10",
            "request_digest": "a" * 64,
            "repository": "owner/repo",
            "ref": "feature/x",
            "authority_event_id": "10",
            "start_sha": "b" * 40,
            "state": "PLANNED",
            "safe_to_blind_retry": False,
            "extensions": {"operation": "format-fix"},
        }

    def test_bot_receipt_round_trips(self):
        receipt = self.receipt()
        body = render_receipt_comment(receipt)
        records = trusted_operation_records(
            [{"author": "github-actions[bot]", "body": body}]
        )
        self.assertEqual(records, [receipt])

    def test_user_cannot_spoof_durable_receipt(self):
        body = render_receipt_comment(self.receipt())
        records = trusted_operation_records([{"author": "owner", "body": body}])
        self.assertEqual(records, [])

    def test_invalid_bot_marker_is_ignored(self):
        records = trusted_operation_records(
            [{"author": "github-actions[bot]", "body": "<!-- UES_OPERATION_RECEIPT:not-json -->"}]
        )
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
