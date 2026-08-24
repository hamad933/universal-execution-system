from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "ues-parent-controller-autowakeup.yml"
)


class ParentControllerValidatedAutoWakeupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_successful_validate_workflow_is_the_automatic_wakeup(self):
        self.assertIn("workflow_run:", self.text)
        self.assertIn('workflows: ["Validate Universal Core"]', self.text)
        self.assertIn("types: [completed]", self.text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.text)
        self.assertIn("github.event.workflow_run.event == 'pull_request'", self.text)
        self.assertIn("github.event.workflow_run.head_branch == 'ues-parent-control'", self.text)
        self.assertNotIn("issue_comment:", self.text)
        self.assertNotIn("pull_request_target:", self.text)

    def test_untrusted_or_stale_control_head_fails_before_request_execution(self):
        self.assertIn("run.head_repository.full_name !== expectedRepo", self.text)
        self.assertIn("run.actor.login !== context.repo.owner", self.text)
        self.assertIn("run.triggering_actor.login !== context.repo.owner", self.text)
        self.assertIn("prs.length !== 1", self.text)
        self.assertIn("control.state !== 'open'", self.text)
        self.assertIn("control.draft !== true", self.text)
        self.assertIn("control.head.ref !== 'ues-parent-control'", self.text)
        self.assertIn("validatedHead !== liveControlHead", self.text)
        self.assertIn("prFiles.length !== 1", self.text)
        self.assertIn("changed.length !== 1", self.text)
        self.assertIn("controlCommit.author.login !== context.repo.owner", self.text)
        self.assertIn("controlCommit.committer.login !== context.repo.owner", self.text)

    def test_only_trusted_default_branch_runtime_is_checked_out_and_revalidated(self):
        self.assertIn("github.rest.repos.getBranch", self.text)
        self.assertIn("ref: ${{ steps.control.outputs.runtime_sha }}", self.text)
        self.assertIn("ref: ${{ needs.preflight.outputs.runtime_sha }}", self.text)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", self.text)
        self.assertIn("--expected-runtime-sha", self.text)
        self.assertIn("Reverify validated runtime is still current before effects", self.text)
        self.assertIn("UES default branch moved after validation", self.text)

    def test_secrets_are_absent_from_preflight_and_present_only_at_effect_boundary(self):
        preflight, execute = self.text.split("\n  execute:\n", 1)
        self.assertNotIn("JULES_API_KEY", preflight)
        self.assertIn("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}", execute)
        self.assertIn("permissions:\n      contents: write\n      issues: write", execute)

    def test_existing_authority_and_initial_lineage_runtimes_are_reused(self):
        self.assertIn("python -m ues.parent_controller_request", self.text)
        self.assertIn("python -m ues.rp_authority_runtime", self.text)
        self.assertIn("python -m ues.lifecycle_runtime_observed", self.text)
        self.assertIn("python -m ues.initial_lineage_runtime", self.text)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON", self.text)
        self.assertIn("UES_AUTHORITY_TRANSPORT_ACTOR", self.text)
        self.assertIn("PARENT_CONTROLLER_VALIDATED_AUTOWAKEUP", self.text)

    def test_durable_receipt_is_sanitized_and_binds_validation_run(self):
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.text)
        self.assertIn("'trigger_kind': 'VALIDATE_WORKFLOW_RUN'", self.text)
        self.assertIn("'validation_run_id'", self.text)
        self.assertIn("'external_effects_dispatched'", self.text)
        self.assertIn("'new_tasks_or_sessions_created'", self.text)
        self.assertIn("'safe_to_blind_retry': False", self.text)
        self.assertIn("'raw_session_ids_persisted': False", self.text)
        self.assertIn("'secret_material_persisted': False", self.text)
        self.assertNotIn("current-authority.json').read_text", self.text)

    def test_project_lifecycle_concurrency_namespace_is_shared(self):
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.preflight.outputs.project }}",
            self.text,
        )
        self.assertIn("cancel-in-progress: false", self.text)


if __name__ == "__main__":
    unittest.main()
