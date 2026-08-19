import unittest

from ues.authority_transport import derive_owner_comment_authority
from ues.idempotency import (
    evaluate_branch_serialization,
    evaluate_idempotency,
    evaluate_write_boundary,
    make_operation_receipt,
)


class AuthorityTransportTests(unittest.TestCase):
    def base(self):
        return dict(
            arguments={
                "operation": "format-fix",
                "sha": "a" * 40,
                "ref": "feature/x",
                "paths": "src/a.py,src/b.py",
                "resources": "workspace",
            },
            actor="owner",
            repository_owner="owner",
            repository="owner/repo",
            pr_number=7,
            comment_id="12345",
            comment_created_at="2026-08-19T19:00:00Z",
            candidate_ref="feature/x",
            candidate_head_sha="a" * 40,
            candidate_tree_sha="b" * 40,
            workstream_id="W1",
        )

    def test_owner_event_derives_operation_id(self):
        result = derive_owner_comment_authority(**self.base())
        self.assertTrue(result["trusted"])
        self.assertEqual(result["operation_id"], "github-comment:12345")
        self.assertEqual(result["authority_envelope"]["expected_head_sha"], "a" * 40)
        self.assertEqual(result["mutation_request"]["proposed_paths"], ["src/a.py", "src/b.py"])

    def test_non_owner_is_rejected(self):
        args = self.base(); args["actor"] = "other"
        with self.assertRaisesRegex(ValueError, "not repository owner"):
            derive_owner_comment_authority(**args)

    def test_stale_sha_is_rejected(self):
        args = self.base(); args["arguments"] = dict(args["arguments"], sha="c" * 40)
        with self.assertRaisesRegex(ValueError, "does not match live candidate HEAD"):
            derive_owner_comment_authority(**args)


class IdempotencyTests(unittest.TestCase):
    def test_new_operation_is_eligible(self):
        result = evaluate_idempotency("op1", "digest", [])
        self.assertEqual(result["decision"], "NEW_OPERATION")
        self.assertTrue(result["safe_to_execute"])

    def test_confirmed_replay_is_not_reexecuted(self):
        record = {"operation_id": "op1", "request_digest": "digest", "state": "CONFIRMED"}
        result = evaluate_idempotency("op1", "digest", [record])
        self.assertEqual(result["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertFalse(result["safe_to_execute"])

    def test_unknown_requires_reconciliation(self):
        record = {"operation_id": "op1", "request_digest": "digest", "state": "UNKNOWN"}
        result = evaluate_idempotency("op1", "digest", [record])
        self.assertEqual(result["decision"], "RECONCILE_REQUIRED")
        self.assertFalse(result["safe_to_blind_retry"])

    def test_digest_collision_is_rejected(self):
        record = {"operation_id": "op1", "request_digest": "other", "state": "CONFIRMED"}
        result = evaluate_idempotency("op1", "digest", [record])
        self.assertEqual(result["decision"], "OPERATION_ID_COLLISION")


class SerializationTests(unittest.TestCase):
    def test_active_other_operation_blocks_same_ref(self):
        records = [{"operation_id": "other", "repository": "owner/repo", "ref": "feature/x", "state": "EXECUTING"}]
        result = evaluate_branch_serialization(repository="owner/repo", ref="feature/x", operation_id="op1", records=records)
        self.assertFalse(result["available"])

    def test_terminal_operation_does_not_block(self):
        records = [{"operation_id": "other", "repository": "owner/repo", "ref": "feature/x", "state": "CONFIRMED"}]
        result = evaluate_branch_serialization(repository="owner/repo", ref="feature/x", operation_id="op1", records=records)
        self.assertTrue(result["available"])


class WriteBoundaryTests(unittest.TestCase):
    def plan(self):
        return {
            "decision": "AUTHORIZED_DRY_RUN",
            "cas": {"live_head_sha": "a" * 40, "live_tree_sha": "b" * 40},
        }

    def test_ready_boundary_remains_non_executable(self):
        result = evaluate_write_boundary(
            mutation_plan=self.plan(), operation_id="op1", request_digest="digest",
            repository="owner/repo", ref="feature/x", live_head_sha="a" * 40,
            live_tree_sha="b" * 40, operation_records=[])
        self.assertTrue(result["ready"])
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["safe_to_execute_now"])

    def test_head_move_blocks_at_immediate_boundary(self):
        result = evaluate_write_boundary(
            mutation_plan=self.plan(), operation_id="op1", request_digest="digest",
            repository="owner/repo", ref="feature/x", live_head_sha="c" * 40,
            live_tree_sha="b" * 40, operation_records=[])
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertIn("WRITE_BOUNDARY_HEAD_MOVED", [f["code"] for f in result["failures"]])

    def test_receipt_is_never_blind_retryable(self):
        receipt = make_operation_receipt(
            operation_id="op1", request_digest="digest", repository="owner/repo",
            ref="feature/x", authority_event_id="123", start_sha="a" * 40,
            start_tree_sha="b" * 40)
        self.assertEqual(receipt["state"], "PLANNED")
        self.assertFalse(receipt["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
