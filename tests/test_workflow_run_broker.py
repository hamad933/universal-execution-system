import unittest
from pathlib import Path


class WorkflowRunBrokerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = Path('.github/workflows/exec-format-fix-broker.yml').read_text(encoding='utf-8')

    def test_uses_trusted_default_branch_workflow_run(self):
        self.assertIn('workflow_run:', self.broker)
        self.assertIn('workflows: ["Validate Universal Core"]', self.broker)
        self.assertIn("github.event.workflow_run.event == 'pull_request'", self.broker)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.broker)
        self.assertNotIn('pull_request_target', self.broker)

    def test_requires_same_repository_owner_command(self):
        self.assertIn('pr.head.repo.full_name !== expectedRepo', self.broker)
        self.assertIn("comment.user.login !== context.repo.owner", self.broker)
        self.assertIn("comment.author_association !== 'OWNER'", self.broker)
        self.assertIn("tokens.includes(`sha=${pr.head.sha}`)", self.broker)
        self.assertIn("tokens.includes(`ref=${pr.head.ref}`)", self.broker)
        self.assertIn('maxAgeMs = 14 * 60 * 1000', self.broker)

    def test_candidate_formatter_has_no_persisted_credentials(self):
        marker = 'Checkout exact candidate without credentials'
        start = self.broker.index(marker)
        fragment = self.broker[start : start + 500]
        self.assertIn('persist-credentials: false', fragment)

    def test_only_apply_job_has_contents_write(self):
        self.assertEqual(self.broker.count('contents: write'), 1)
        apply_index = self.broker.index('  apply:')
        write_index = self.broker.index('contents: write')
        self.assertGreater(write_index, apply_index)

    def test_apply_keeps_cas_normal_push_path(self):
        self.assertIn('Apply patch with immediate CAS and normal push', self.broker)
        self.assertNotIn('--force', self.broker)
        self.assertNotIn('force-with-lease', self.broker)

    def test_terminal_receipt_is_durable(self):
        self.assertIn('Publish final durable receipt', self.broker)
        self.assertIn('resolved-receipt.json', self.broker)
        self.assertIn('retention-days: 7', self.broker)


if __name__ == '__main__':
    unittest.main()
