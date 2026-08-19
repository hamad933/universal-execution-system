import unittest
from datetime import datetime, timezone

from ues.transaction import active_lease_conflicts, plan_mutation, reconcile_post_write, validate_authority


NOW = datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc)


def envelope():
    return {
        "schema_version": "0.4",
        "operation_id": "op-1",
        "workstream_id": "W1",
        "repository": "owner/repo",
        "ref": "feature/x",
        "expected_head_sha": "a" * 40,
        "expected_tree_sha": "b" * 40,
        "operation": "format-fix",
        "allowed_paths": ["src/**", "package.json"],
        "prohibited_paths": ["src/secrets/**"],
        "resource_classes": ["ref:feature/x"],
        "expires_at": "2026-08-19T20:00:00+00:00",
        "stop_gate": "HEAD_MOVED",
        "write_policy": {"max_changed_paths": 3},
    }


class AuthorityTests(unittest.TestCase):
    def test_exact_authority_passes(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["src/a/b.py"],
            resource_classes=["ref:feature/x"],
            expected_operation_id="op-1",
            expected_workstream_id="W1",
            now=NOW,
        )
        self.assertTrue(result["valid"])

    def test_head_mismatch_fails_closed(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="c" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["src/app.py"],
            now=NOW,
        )
        self.assertIn("HEAD_MISMATCH", [item["code"] for item in result["failures"]])

    def test_prohibited_path_wins(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["src/secrets/key.txt"],
            now=NOW,
        )
        self.assertIn("PROHIBITED_PATH", [item["code"] for item in result["failures"]])

    def test_path_escape_is_rejected(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["../outside"],
            now=NOW,
        )
        self.assertIn("UNSAFE_PATH", [item["code"] for item in result["failures"]])

    def test_resource_outside_authority_is_rejected(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["src/app.py"],
            resource_classes=["secret:prod"],
            now=NOW,
        )
        self.assertIn("RESOURCE_OUTSIDE_AUTHORITY", [item["code"] for item in result["failures"]])

    def test_operation_id_binding_is_enforced(self):
        result = validate_authority(
            envelope(),
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation="format-fix",
            proposed_paths=["src/app.py"],
            expected_operation_id="different-op",
            now=NOW,
        )
        self.assertIn("OPERATION_ID_MISMATCH", [item["code"] for item in result["failures"]])


class LeaseTests(unittest.TestCase):
    def test_active_path_lease_conflicts(self):
        conflicts = active_lease_conflicts(
            repository="owner/repo",
            ref="feature/x",
            operation_id="op-1",
            proposed_paths=["src/a/b.py"],
            resource_classes=[],
            active_leases=[{
                "lease_id": "lease-2",
                "operation_id": "op-2",
                "repository": "owner/repo",
                "ref": "feature/x",
                "path_patterns": ["src/**"],
                "resource_classes": [],
                "expires_at": "2026-08-19T20:00:00+00:00",
                "state": "ACTIVE",
            }],
            now=NOW,
        )
        self.assertEqual(conflicts[0]["code"], "LEASE_CONFLICT")

    def test_released_lease_does_not_conflict(self):
        conflicts = active_lease_conflicts(
            repository="owner/repo",
            ref="feature/x",
            operation_id="op-1",
            proposed_paths=["src/app.py"],
            resource_classes=[],
            active_leases=[{
                "lease_id": "lease-2",
                "operation_id": "op-2",
                "repository": "owner/repo",
                "ref": "feature/x",
                "path_patterns": ["src/**"],
                "resource_classes": [],
                "state": "RELEASED",
            }],
            now=NOW,
        )
        self.assertEqual(conflicts, [])


class MutationPlanTests(unittest.TestCase):
    def test_authorized_plan_remains_non_executable(self):
        result = plan_mutation(
            envelope(),
            {
                "operation": "format-fix",
                "proposed_paths": ["src/app.py"],
                "resource_classes": ["ref:feature/x"],
            },
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            operation_id="op-1",
            workstream_id="W1",
            now=NOW,
        )
        self.assertEqual(result["decision"], "AUTHORIZED_DRY_RUN")
        self.assertTrue(result["eligible_for_future_execution"])
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["safe_to_execute_now"])
        self.assertEqual(result["retry_policy"], "RECONCILE_LIVE_STATE_BEFORE_ANY_RETRY")

    def test_lease_conflict_rejects_plan(self):
        result = plan_mutation(
            envelope(),
            {"operation": "format-fix", "proposed_paths": ["src/app.py"], "resource_classes": []},
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            active_leases=[{
                "lease_id": "lease-2",
                "operation_id": "op-2",
                "repository": "owner/repo",
                "ref": "feature/x",
                "path_patterns": ["src/**"],
                "resource_classes": [],
                "expires_at": "2026-08-19T20:00:00+00:00",
                "state": "ACTIVE",
            }],
            now=NOW,
        )
        self.assertEqual(result["decision"], "REJECTED")


class ReconcileTests(unittest.TestCase):
    def test_changed_unknown_post_state_never_blindly_retries(self):
        plan = plan_mutation(
            envelope(),
            {"operation": "format-fix", "proposed_paths": ["src/app.py"], "resource_classes": []},
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            now=NOW,
        )
        result = reconcile_post_write(
            plan,
            live_head_sha="c" * 40,
            live_tree_sha="d" * 40,
            observed_changed_paths=["src/app.py"],
        )
        self.assertEqual(result["verdict"], "POST_STATE_REQUIRES_RECONCILIATION")
        self.assertFalse(result["safe_to_blind_retry"])

    def test_unexpected_path_is_detected(self):
        plan = plan_mutation(
            envelope(),
            {"operation": "format-fix", "proposed_paths": ["src/app.py"], "resource_classes": []},
            repository="owner/repo",
            ref="feature/x",
            live_head_sha="a" * 40,
            live_tree_sha="b" * 40,
            now=NOW,
        )
        result = reconcile_post_write(
            plan,
            live_head_sha="c" * 40,
            live_tree_sha="d" * 40,
            observed_changed_paths=["src/app.py", "secrets.txt"],
        )
        self.assertEqual(result["verdict"], "UNAUTHORIZED_POST_WRITE_PATHS")


if __name__ == "__main__":
    unittest.main()
