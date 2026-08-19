import unittest
from pathlib import Path


class WriteWorkflowSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.write = Path(".github/workflows/exec-format-fix.yml").read_text(encoding="utf-8")
        cls.readonly = Path(".github/workflows/exec-readonly.yml").read_text(encoding="utf-8")

    def test_write_workflow_never_uses_pull_request_target(self):
        self.assertNotIn("pull_request_target", self.write)

    def test_fork_prs_are_rejected(self):
        self.assertIn("UES write workflow rejects fork PRs", self.write)
        self.assertIn("pr.head.repo.full_name", self.write)

    def test_candidate_formatter_checkout_does_not_persist_credentials(self):
        marker = "Checkout exact candidate without credentials"
        start = self.write.index(marker)
        fragment = self.write[start : start + 500]
        self.assertIn("persist-credentials: false", fragment)

    def test_write_permission_is_scoped_to_apply_job(self):
        self.assertEqual(self.write.count("contents: write"), 1)
        apply_index = self.write.index("  apply:")
        write_index = self.write.index("contents: write")
        self.assertGreater(write_index, apply_index)

    def test_readonly_bridge_excludes_format_fix(self):
        self.assertIn("!startsWith(github.event.comment.body, '/exec format-fix')", self.readonly)

    def test_normal_push_only_no_force(self):
        self.assertNotIn("--force", self.write)
        self.assertNotIn("force-with-lease", self.write)

    def test_safe_recovery_receipts_are_published_durably(self):
        self.assertIn("recovery_receipts", self.write)
        self.assertIn("Publish recovered durable receipts", self.write)
        self.assertIn("render_receipt_comment", self.write)


if __name__ == "__main__":
    unittest.main()
