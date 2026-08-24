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

    def test_relay_is_secretless_and_does_not_emit_second_event(self):
        relay = self.validate.split("\n  parent-controller-relay:\n", 1)[1].split("\n  parent-controller-receiver:\n", 1)[0]
        self.assertIn("contents: read", relay)
        self.assertNotIn("actions: write", relay)
        self.assertNotIn("contents: write", relay)
        self.assertNotIn("JULES_API_KEY", relay)
        self.assertNotIn("UES_CURRENT_AUTHORITY_JSON", relay)
        self.assertNotIn("createWorkflowDispatch", relay)
        self.assertNotIn("repository_dispatch", relay)

    def test_validate_calls_receiver_as_same_run_reusable_workflow(self):
        self.assertIn("parent-controller-receiver:", self.validate)
        receiver_call = self.validate.split("\n  parent-controller-receiver:\n", 1)[1]
        self.assertIn("needs: [core, parent-controller-relay]", receiver_call)
        self.assertIn("needs.parent-controller-relay.result == 'success'", receiver_call)
        self.assertIn("uses: ./.github/workflows/ues-parent-controller-dispatch.yml", receiver_call)
        self.assertIn("control_head: ${{ github.event.pull_request.head.sha }}", receiver_call)
        self.assertIn("validation_run_id: ${{ github.run_id }}", receiver_call)
        self.assertIn("control_pr_number: ${{ github.event.pull_request.number }}", receiver_call)
        self.assertIn("secrets: inherit", receiver_call)
        self.assertIn("actions: read", receiver_call)
        self.assertIn("contents: write", receiver_call)
        self.assertIn("issues: write", receiver_call)

    def test_receiver_is_workflow_call_only_and_keeps_no_event_ingress(self):
        trigger = self.receiver.split("permissions:", 1)[0]
        self.assertIn("workflow_call:", trigger)
        self.assertNotIn("workflow_dispatch:", trigger)
        self.assertNotIn("pull_request:\n", trigger)
        self.assertNotIn("pull_request_target:", trigger)
        self.assertNotIn("issue_comment:", trigger)
        self.assertNotIn("workflow_run:", trigger)
        self.assertIn("control_head:", self.receiver)
        self.assertIn("validation_run_id:", self.receiver)
        self.assertIn("control_pr_number:", self.receiver)
        self.assertIn("group: ues-parent-controller-control-queue-${{ inputs.control_head }}", self.receiver)
        self.assertIn("cancel-in-progress: false", self.receiver)

    def test_receiver_start_is_durably_visible_before_preflight(self):
        self.assertIn("announce:", self.receiver)
        self.assertIn("Publish durable receiver-start receipt", self.receiver)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIVER_STARTED_V1", self.receiver)
        self.assertIn("receiver_run_id: Number(context.runId)", self.receiver)
        self.assertIn("receiver_mode: 'SAME_RUN_REUSABLE_WORKFLOW'", self.receiver)
        self.assertIn("stage: 'RECEIVER_STARTED_PRE_EFFECT'", self.receiver)
        self.assertIn("effect_job_reached: false", self.receiver)
        self.assertIn("safe_to_blind_retry: false", self.receiver)
        self.assertIn("needs: announce", self.receiver)

    def test_receiver_revalidates_exact_control_pr_commit_and_validation_run(self):
        for expected in (
            "github.rest.pulls.get",
            "control.draft !== true",
            "control.head.ref !== 'ues-parent-control'",
            "control.base.ref !== defaultBranch",
            "prFiles.length !== 1",
            "controlCommit.author.login !== context.repo.owner",
            "controlCommit.committer.login !== context.repo.owner",
            "validation.name !== 'Validate Universal Core'",
            "validation.head_branch !== 'ues-parent-control'",
            "validation.actor.login !== context.repo.owner",
            "validation.triggering_actor.login !== context.repo.owner",
            "validationPrs.length !== 1",
            "Number(validationPrs[0].number) !== prNumber",
        ):
            self.assertIn(expected, self.receiver)

    def test_receiver_binds_to_completed_successful_core_job_not_full_run_race(self):
        self.assertIn("github.rest.actions.listJobsForWorkflowRun", self.receiver)
        self.assertIn("job.name === 'core'", self.receiver)
        self.assertIn("job.status === 'completed'", self.receiver)
        self.assertIn("job.conclusion === 'success'", self.receiver)
        self.assertIn("coreJobs.length !== 1", self.receiver)
        self.assertIn("['in_progress', 'completed'].includes(validation.status)", self.receiver)
        self.assertIn("validation.status === 'completed' && validation.conclusion !== 'success'", self.receiver)

    def test_preflight_failure_is_durably_visible_and_never_claims_effect(self):
        self.assertIn("preflight-failure-receipt:", self.receiver)
        self.assertIn("if: always() && needs.preflight.result != 'success'", self.receiver)
        self.assertIn("UES_PARENT_CONTROLLER_PREFLIGHT_FAILURE_V1", self.receiver)
        self.assertIn("receiver_run_id: Number(context.runId)", self.receiver)
        self.assertIn("stage: 'PRE_EFFECT_PREFLIGHT'", self.receiver)
        self.assertIn("effect_job_reached: false", self.receiver)
        failure = self.receiver.split("preflight-failure-receipt:", 1)[1].split("\n  execute:\n", 1)[0]
        self.assertNotIn("secrets.JULES_API_KEY", failure)
        self.assertNotIn("UES_CURRENT_AUTHORITY_JSON", failure)
        self.assertNotIn("contents: write", failure)

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

    def test_secrets_are_used_only_in_effect_job_after_preflight(self):
        pre_effect, execute = self.receiver.split("\n  execute:\n", 1)
        self.assertNotIn("secrets.JULES_API_KEY", pre_effect)
        self.assertNotIn("contents: write", pre_effect)
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

    def test_final_receipt_is_sanitized_and_same_run_bound(self):
        self.assertIn("'trigger_kind': 'VALIDATE_REUSABLE_WORKFLOW_CALL'", self.receiver)
        self.assertIn("'validation_run_id'", self.receiver)
        self.assertIn("'receiver_run_id'", self.receiver)
        self.assertIn("UES_RECEIVER_RUN_ID: ${{ github.run_id }}", self.receiver)
        self.assertIn("'external_effects_dispatched'", self.receiver)
        self.assertIn("'new_tasks_or_sessions_created'", self.receiver)
        self.assertIn("'raw_session_ids_persisted': False", self.receiver)
        self.assertIn("'secret_material_persisted': False", self.receiver)
        receipt = self.receiver.split("Render sanitized durable Parent Controller receipt", 1)[1]
        self.assertNotIn("current-authority.json').read_text", receipt)
        self.assertNotIn("secrets.JULES_API_KEY", receipt)

    def test_unproven_workflow_run_fast_path_is_removed(self):
        self.assertFalse(OLD_AUTOWAKEUP.exists())


if __name__ == "__main__":
    unittest.main()
