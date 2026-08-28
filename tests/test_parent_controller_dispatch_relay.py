from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"
DISPATCH = ROOT / ".github" / "workflows" / "ues-parent-controller-dispatch.yml"


class ParentControllerInlinePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = VALIDATE.read_text(encoding="utf-8")

    def test_cross_workflow_dispatch_receiver_is_retired(self):
        self.assertFalse(DISPATCH.exists())
        self.assertNotIn("createWorkflowDispatch", self.text)
        self.assertNotIn("workflow_dispatch", self.text)

    def test_parent_preflight_runs_independently_for_exact_control_pr(self):
        self.assertIn("parent-controller-preflight:", self.text)
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertNotIn("needs: core", preflight.split("\n    steps:\n", 1)[0])
        self.assertIn("github.event_name == 'pull_request'", preflight)
        self.assertIn("github.event.pull_request.head.ref == 'ues-parent-control'", preflight)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", preflight)
        self.assertIn('PR_DRAFT: ${{ github.event.pull_request.draft }}', preflight)
        self.assertIn('git diff --name-only "$BASE_SHA...$CONTROL_HEAD"', preflight)
        self.assertIn(
            "Persistent Parent Controller PR must contain at least one semantic request slot",
            preflight,
        )
        self.assertIn("Persistent Parent Controller PR contains non-transport path", preflight)
        self.assertNotIn("github.rest.pulls.listFiles", preflight)
        self.assertIn("controlCommit.author.login !== context.repo.owner", preflight)
        self.assertIn("controlCommit.committer.login !== context.repo.owner", preflight)

    def test_preflight_is_read_only_and_has_no_provider_secret(self):
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertIn("contents: read", preflight)
        self.assertIn("issues: read", preflight)
        self.assertIn("pull-requests: read", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertNotIn("JULES_API_KEY", preflight)
        self.assertNotIn("secrets.JULES_API_KEY", preflight)

    def test_control_branch_is_data_only_and_live_main_runtime_is_used(self):
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertIn('LEGACY_REQUEST_PATH: .ues/parent-controller-request.json', preflight)
        self.assertIn('.ues/parent-controller-requests', preflight)
        self.assertIn('git rev-parse "$CONTROL_HEAD:$REQUEST_PATH"', preflight)
        self.assertIn('git hash-object "$RUNNER_TEMP/parent-controller-request.json"', preflight)
        self.assertIn('git ls-remote origin "refs/heads/$DEFAULT_BRANCH"', preflight)
        self.assertNotIn("github.rest.repos.getContent", preflight)
        self.assertNotIn("github.rest.repos.getBranch", preflight)
        self.assertIn("ref: ${{ steps.control.outputs.runtime_sha }}", self.text)
        self.assertIn("--expected-runtime-sha", self.text)
        self.assertIn("python -m ues.parent_controller_request", self.text)

    def test_preflight_failure_is_durable_and_pre_effect(self):
        self.assertIn("parent-controller-preflight-failure:", self.text)
        self.assertIn("UES_PARENT_CONTROLLER_PREFLIGHT_FAILURE_V1", self.text)
        self.assertIn("effect_job_reached: false", self.text)
        self.assertIn("safe_to_blind_retry: false", self.text)
        failure = self.text.split("\n  parent-controller-preflight-failure:\n", 1)[1].split(
            "\n  parent-controller-execute:\n", 1
        )[0]
        self.assertNotIn("JULES_API_KEY", failure)
        self.assertNotIn("contents: write", failure)

    def test_effect_job_is_separate_project_serialized_boundary(self):
        execute = self.text.split("\n  parent-controller-execute:\n", 1)[1]
        self.assertIn("needs: [core, parent-controller-preflight]", execute)
        self.assertIn("needs.core.result == 'success'", execute)
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}",
            execute,
        )
        self.assertIn("cancel-in-progress: false", execute)
        self.assertIn("permissions:\n      contents: write\n      issues: write", execute)
        self.assertIn("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}", execute)

    def test_live_main_is_reverified_before_effect_secret_step(self):
        execute = self.text.split("\n  parent-controller-execute:\n", 1)[1]
        drift = execute.index("Reverify validated runtime is still current before effects")
        secret = execute.index("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}")
        self.assertLess(drift, secret)
        self.assertIn("git ls-remote origin", execute)
        self.assertIn("UES default branch moved after Parent Controller preflight", execute)

    def test_existing_governed_runtime_is_reused(self):
        execute = self.text.split("\n  parent-controller-execute:\n", 1)[1]
        self.assertIn("python -m ues.rp_authority_runtime", execute)
        self.assertIn("python -m ues.lifecycle_runtime_observed", execute)
        self.assertIn("python -m ues.initial_lineage_runtime", execute)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON", execute)
        self.assertIn("PARENT_CONTROLLER_VALIDATED_INLINE", execute)

    def test_duplicate_receipt_suppresses_reexecution(self):
        self.assertIn("Suppress already-receipted request", self.text)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.text)
        self.assertIn("request_digest", self.text)
        self.assertIn("already_receipted", self.text)
        execute = self.text.split("\n  parent-controller-execute:\n", 1)[1]
        self.assertIn(
            "needs.parent-controller-preflight.outputs.rate_limit_deferred != 'true'",
            execute,
        )
        self.assertIn(
            "needs.parent-controller-preflight.outputs.already_receipted != 'true'",
            execute,
        )

    def test_final_receipt_is_sanitized_and_run_bound(self):
        self.assertIn("'trigger_kind': 'VALIDATE_INLINE_PARENT_PIPELINE'", self.text)
        self.assertIn("'validation_run_id'", self.text)
        self.assertIn("'external_effects_dispatched'", self.text)
        self.assertIn("'new_tasks_or_sessions_created'", self.text)
        self.assertIn("'safe_to_blind_retry': False", self.text)
        self.assertIn("'raw_session_ids_persisted': False", self.text)
        self.assertIn("'secret_material_persisted': False", self.text)
        receipt = self.text.split("Render sanitized durable Parent Controller receipt", 1)[1]
        self.assertNotIn("current-authority.json').read_text", receipt)
        self.assertNotIn("JULES_API_KEY", receipt)


if __name__ == "__main__":
    unittest.main()
