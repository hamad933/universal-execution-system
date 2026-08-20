import unittest
from pathlib import Path


class ControlQueueSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(
            ".github/workflows/exec-format-fix-control-queue.yml"
        ).read_text(encoding="utf-8")

    def test_uses_trusted_control_pr_synchronize_signal(self):
        self.assertIn("pull_request_target:", self.workflow)
        self.assertIn("types: [synchronize]", self.workflow)
        self.assertIn("head.ref == 'ues-control'", self.workflow)
        self.assertIn("head.repo.full_name == github.repository", self.workflow)

    def test_control_request_is_not_authority(self):
        self.assertIn(".ues/request.json", self.workflow)
        self.assertIn("command.user.login !== context.repo.owner", self.workflow)
        self.assertIn("command.author_association !== 'OWNER'", self.workflow)
        self.assertIn("authority comment is not /exec format-fix", self.workflow)
        self.assertIn("authority does not match live target SHA/ref", self.workflow)
        self.assertIn("authority comment is expired or invalid", self.workflow)

    def test_target_is_exact_and_same_repository(self):
        self.assertIn("pr.head.repo.full_name !== expectedRepo", self.workflow)
        self.assertIn("expectedSha !== pr.head.sha", self.workflow)
        self.assertIn("expectedRef !== pr.head.ref", self.workflow)
        self.assertIn("target PR must be open", self.workflow)

    def test_formatter_sandbox_has_no_persisted_credentials(self):
        marker = "Checkout exact candidate without credentials"
        start = self.workflow.index(marker)
        fragment = self.workflow[start : start + 550]
        self.assertIn("persist-credentials: false", fragment)

    def test_only_apply_job_has_contents_write(self):
        self.assertEqual(self.workflow.count("contents: write"), 1)
        apply_index = self.workflow.index("  apply:")
        write_index = self.workflow.index("contents: write")
        self.assertGreater(write_index, apply_index)

    def test_apply_uses_trusted_cas_executor_without_force(self):
        apply_start = self.workflow.index("  apply:")
        finalize_start = self.workflow.index("  finalize:")
        fragment = self.workflow[apply_start:finalize_start]
        self.assertIn("python -m ues.write_cli apply", fragment)
        self.assertNotIn("--force", fragment)
        self.assertNotIn("force-with-lease", fragment)
        self.assertNotIn("npm run", fragment)
        self.assertNotIn("composer", fragment)
        self.assertNotIn("node_modules/.bin", fragment)

    def test_durable_receipts_are_retained(self):
        self.assertIn("Publish initial durable receipt", self.workflow)
        self.assertIn("Publish recovered durable receipts", self.workflow)
        self.assertIn("Publish final durable receipt", self.workflow)
        self.assertIn("retention-days: 7", self.workflow)


if __name__ == "__main__":
    unittest.main()
