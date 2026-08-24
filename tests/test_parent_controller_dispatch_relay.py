from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"
RECEIVER = ROOT / ".github" / "workflows" / "ues-parent-controller-dispatch.yml"
OLD_AUTOWAKEUP = ROOT / ".github" / "workflows" / "ues-parent-controller-autowakeup.yml"


class ParentControllerValidatedDispatchRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validate = VALIDATE.read_text(encoding="utf-8")
        cls.receiver = RECEIVER.read_text(encoding="utf-8")

    def test_relay_runs_only_after_core_on_exact_control_pr(self):
        self.assertIn("parent-controller-relay:", self.validate)
        self.assertIn("needs: core", self.validate)
        self.assertIn("github.event_name == 'pull_request'", self.validate)
        self.assertIn("github.event.pull_request.head.ref == 'ues-parent-control'", self.validate)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", self.validate)
        self.assertIn("pr.draft !== true", self.validate)
        self.assertIn("prFiles.length !== 1", self.validate)
        self.assertIn("commit.author.login !== context.repo.owner", self.validate)
        self.assertIn("commit.committer.login !== context.repo.owner", self.validate)

    def test_relay_is_secretless_and_can_only_dispatch_actions(self):
        relay = self.validate.split("\n  parent-controller-relay:\n", 1)[1]
        self.assertIn("actions: write", relay)
        self.assertIn("contents: read", relay)
        self.assertNotIn("contents: write", relay)
        self.assertNotIn("JULES_API_KEY", relay)
        self.assertNotIn("UES_CURRENT_AUTHORITY_JSON", relay)
        self.assertIn("github.rest.actions.createWorkflowDispatch", relay)
        self.assertIn("workflow_id: 'ues-parent-controller-dispatch.yml'", relay)
        self.assertIn("control_head: exactHead", relay)
        self.assertIn("validation_run_id", relay)

    def test_receiver_is_trusted_workflow_dispatch_only(self):
        self.assertIn("workflow_dispatch:", self.receiver)
        self.assertNotIn("pull_request:", self.receiver.split("permissions:", 1)[0])
        self.assertNotIn("pull_request_target:", self.receiver.split("permissions:", 1)[0])
        self.assertNotIn("issue_comment:", self.receiver.split("permissions:", 1)[0])
        self.assertNotIn("workflow_run:", self.receiver.split("permissions:", 1)[0])
        self.assertIn("control_head:", self.receiver)
        self.assertIn("validation_run_id:", self.receiver)
        self.assertIn("group: ues-parent-controller-control-queue-${{ github.repository }}", self.receiver)
        self.assertIn("cancel-in-progress: false", self.receiver)

    def test_receiver_revalidates_control_pr_commit_and_validation_run(self):
        for expected in (
            "Exactly one persistent Parent Controller control PR must be open",
            "control.draft !== true",
            "control.head.ref !== 'ues-parent-control'",
            "prFiles.length !== 1",
            "controlCommit.author.login !== context.repo.owner",
            "controlCommit.committer.login !== context.repo.owner",
            "validation.name !== 'Validate Universal Core'",
            "validation.conclusion !== 'success'",
            "validation.head_branch !== 'ues-parent-control'",
            "validation.actor.login !== context.repo.owner",
            "validation.triggering_actor.login !== context.repo.owner",
            "validationPrs.length !== 1",
        ):
            self.assertIn(expected, self.receiver)

    def test_receiver_executes_only_trusted_live_runtime_after_semantic_validation(self):
        self.assertIn("github.rest.repos.getBranch", self.receiver)
        self.assertIn("ref: ${{ steps.control.outputs.runtime_sha }}", self.receiver)
        self.assertIn("ref: ${{ needs.preflight.outputs.runtime_sha }}", self.receiver)
        self.assertNotIn("ref: ${{ inputs.control_head }}", self.receiver)
        self.assertIn("python -m ues.parent_controller_request", self.receiver)
        self.assertIn("--expected-runtime-sha", self.receiver)
        self.assertIn("Reverify validated runtime is still current before effects", self.receiver)
        self.assertIn("python -m ues.rp_authority_runtime", self.receiver)
        self.assertIn("python -m ues.lifecycle_runtime_observed", self.receiver)
        self.assertIn("python -m ues.initial_lineage_runtime", self.receiver)

    def test_secrets_exist_only_in_effect_job_after_preflight(self):
        preflight, execute = self.receiver.split("\n  execute:\n", 1)
        self.assertNotIn("JULES_API_KEY", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertIn("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}", execute)
        self.assertIn("permissions:\n      contents: write\n      issues: write", execute)
        drift = execute.index("Reverify validated runtime is still current before effects")
        secret = execute.index("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}")
        self.assertLess(drift, secret)

    def test_duplicate_receipt_suppresses_second_execution(self):
        self.assertIn("Suppress already-receipted request", self.receiver)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.receiver)
        self.assertIn("request_digest", self.receiver)
        self.assertIn("already_receipted", self.receiver)
        self.assertIn("if: needs.preflight.outputs.already_receipted != 'true'", self.receiver)
        self.assertIn("'safe_to_blind_retry': False", self.receiver)

    def test_receipt_is_sanitized_and_validation_bound(self):
        self.assertIn("'trigger_kind': 'VALIDATE_WORKFLOW_DISPATCH'", self.receiver)
        self.assertIn("'validation_run_id'", self.receiver)
        self.assertIn("'external_effects_dispatched'", self.receiver)
        self.assertIn("'new_tasks_or_sessions_created'", self.receiver)
        self.assertIn("'raw_session_ids_persisted': False", self.receiver)
        self.assertIn("'secret_material_persisted': False", self.receiver)
        receipt = self.receiver.split("Render sanitized durable Parent Controller receipt", 1)[1]
        self.assertNotIn("current-authority.json').read_text", receipt)
        self.assertNotIn("JULES_API_KEY", receipt)

    def test_unproven_workflow_run_fast_path_is_removed(self):
        self.assertFalse(OLD_AUTOWAKEUP.exists())


if __name__ == "__main__":
    unittest.main()
