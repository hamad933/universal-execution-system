from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"


class ParentControllerRateLimitHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = VALIDATE.read_text(encoding="utf-8")
        cls.preflight = cls.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        cls.execute = cls.text.split("\n  parent-controller-execute:\n", 1)[1]

    def test_primary_rate_limit_is_checked_before_rest_backed_trust_calls(self):
        self.assertIn("github.rest.rateLimit.get()", self.preflight)
        self.assertIn("UES_PARENT_CONTROLLER_RATE_LIMIT_DEFERRAL_V1", self.preflight)
        self.assertIn("DEFERRED_GITHUB_RATE_LIMIT", self.preflight)
        self.assertIn("external_effects_dispatched: 0", self.preflight)
        self.assertIn("new_tasks_or_sessions_created: 0", self.preflight)
        self.assertIn("safe_to_blind_retry: false", self.preflight)

    def test_preflight_uses_local_git_for_non_identity_repository_reads(self):
        self.assertIn("git diff --name-only", self.preflight)
        self.assertIn("git diff-tree --no-commit-id --name-only", self.preflight)
        self.assertIn("git hash-object", self.preflight)
        self.assertIn("git ls-remote origin", self.preflight)
        self.assertNotIn("github.rest.pulls.listFiles", self.preflight)
        self.assertNotIn("github.rest.repos.getBranch", self.preflight)
        self.assertNotIn("github.rest.repos.getContent", self.preflight)

    def test_owner_identity_and_duplicate_suppression_are_not_weakened(self):
        self.assertIn("github.rest.repos.getCommit", self.preflight)
        self.assertIn("controlCommit.author.login !== context.repo.owner", self.preflight)
        self.assertIn("controlCommit.committer.login !== context.repo.owner", self.preflight)
        self.assertIn("github.rest.issues.listComments", self.preflight)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.preflight)

    def test_effect_job_is_blocked_when_rate_limit_guard_defers(self):
        self.assertIn("rate_limit_deferred", self.text)
        self.assertIn(
            "needs.parent-controller-preflight.outputs.rate_limit_deferred != 'true'",
            self.execute,
        )
        self.assertIn("group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}", self.execute)
        self.assertIn("cancel-in-progress: false", self.execute)

    def test_live_runtime_recheck_no_longer_spends_core_rest_budget(self):
        self.assertIn("Reverify validated runtime is still current before effects", self.execute)
        self.assertIn("git ls-remote origin", self.execute)
        self.assertNotIn("github.rest.repos.getBranch", self.execute)

    def test_rate_limit_telemetry_is_carried_into_canonical_receipt(self):
        self.assertIn("github-rate-limit.json", self.text)
        self.assertIn("github_rate_limit_preflight", self.execute)
        self.assertIn("Preserve durable Parent Controller receipt evidence", self.execute)


if __name__ == "__main__":
    unittest.main()
