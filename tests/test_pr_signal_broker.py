import unittest
from pathlib import Path


class PrSignalBrokerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(
            ".github/workflows/exec-format-fix-pr-signal.yml"
        ).read_text(encoding="utf-8")

    def test_uses_default_branch_pr_metadata_signal_only(self):
        self.assertIn("pull_request_target:", self.workflow)
        self.assertIn("types: [edited]", self.workflow)
        self.assertIn("UES_EXECUTE:", self.workflow)

    def test_signal_is_not_authority(self):
        self.assertIn("command.user.login !== context.repo.owner", self.workflow)
        self.assertIn("comment.author_association !== 'OWNER'", self.workflow)
        self.assertIn("authority comment is not /exec format-fix", self.workflow)
        self.assertIn("authority does not match live PR SHA/ref", self.workflow)
        self.assertIn("authority comment is expired or invalid", self.workflow)

    def test_fork_prs_are_rejected_before_candidate_checkout(self):
        self.assertIn("pr.head.repo.full_name !== expectedRepo", self.workflow)
        self.assertIn("UES PR signal rejects fork PRs", self.workflow)

    def test_candidate_formatter_checkout_has_no_credentials(self):
        marker = "Checkout exact candidate without credentials"
        start = self.workflow.index(marker)
        fragment = self.workflow[start : start + 550]
        self.assertIn("persist-credentials: false", fragment)

    def test_only_apply_job_has_contents_write(self):
        self.assertEqual(self.workflow.count("contents: write"), 1)
        apply_index = self.workflow.index("  apply:")
        write_index = self.workflow.index("contents: write")
        self.assertGreater(write_index, apply_index)

    def test_apply_uses_existing_cas_executor_and_no_force_push(self):
        self.assertIn("python -m ues.write_cli apply", self.workflow)
        self.assertNotIn("--force", self.workflow)
        self.assertNotIn("force-with-lease", self.workflow)

    def test_durable_receipts_are_published(self):
        self.assertIn("Publish initial durable receipt", self.workflow)
        self.assertIn("Publish final durable receipt", self.workflow)
        self.assertIn("Publish recovered durable receipts", self.workflow)

    def test_candidate_code_is_never_invoked_in_apply_job(self):
        apply_start = self.workflow.index("  apply:")
        finalize_start = self.workflow.index("  finalize:")
        apply_fragment = self.workflow[apply_start:finalize_start]
        self.assertNotIn("npm run", apply_fragment)
        self.assertNotIn("composer", apply_fragment)
        self.assertNotIn("./vendor/", apply_fragment)
        self.assertNotIn("node_modules/.bin", apply_fragment)
        self.assertIn("python -m ues.write_cli apply", apply_fragment)


if __name__ == "__main__":
    unittest.main()
