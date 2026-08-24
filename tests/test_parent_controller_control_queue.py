from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-parent-controller-control-queue.yml"


class ParentControllerControlQueueWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_uses_dedicated_trusted_pull_request_target_control_queue_only(self):
        self.assertIn("pull_request_target:", self.text)
        self.assertIn("types: [synchronize]", self.text)
        self.assertIn("github.event.pull_request.head.ref == 'ues-parent-control'", self.text)
        self.assertIn("control.head.ref !== 'ues-parent-control'", self.text)
        self.assertNotIn("head.ref == 'ues-control'", self.text)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", self.text)
        self.assertIn("github.event.pull_request.base.ref == github.event.repository.default_branch", self.text)

    def test_signal_requires_owner_sender_and_full_delta_is_single_path(self):
        self.assertIn("context.payload.sender.login !== context.repo.owner", self.text)
        self.assertIn("github.rest.repos.compareCommits", self.text)
        self.assertIn("base: before", self.text)
        self.assertIn("head: control.head.sha", self.text)
        self.assertIn("eventFiles.length !== 1", self.text)
        self.assertIn("synchronize delta must change only .ues/parent-controller-request.json", self.text)

    def test_control_commit_is_owner_authored_and_single_path(self):
        self.assertIn("controlCommit.author.login !== context.repo.owner", self.text)
        self.assertIn("controlCommit.committer.login !== context.repo.owner", self.text)
        self.assertIn("files.length !== 1", self.text)
        self.assertIn(".ues/parent-controller-request.json", self.text)
        self.assertIn("request file did not change", self.text)

    def test_request_is_bound_to_exact_live_default_branch_runtime(self):
        self.assertIn("github.rest.repos.getBranch", self.text)
        self.assertIn("runtime_sha", self.text)
        self.assertIn("--expected-runtime-sha", self.text)
        self.assertIn("ref: ${{ steps.control.outputs.runtime_sha }}", self.text)
        self.assertIn("ref: ${{ needs.preflight.outputs.runtime_sha }}", self.text)

    def test_runtime_is_reverified_current_again_immediately_before_effects(self):
        self.assertIn("Reverify validated runtime is still current before effects", self.text)
        self.assertIn("EXPECTED_RUNTIME_SHA", self.text)
        self.assertIn("UES default branch moved after preflight", self.text)
        reverify = self.text.index("Reverify validated runtime is still current before effects")
        secret = self.text.index("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}")
        self.assertLess(reverify, secret)

    def test_untrusted_control_branch_is_never_checked_out_or_executed(self):
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha }}", self.text)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.ref }}", self.text)
        self.assertIn("github.rest.repos.getContent", self.text)
        self.assertIn("Parent Controller request file is missing or invalid", self.text)

    def test_secrets_exist_only_in_effect_job_after_preflight(self):
        preflight, execute = self.text.split("\n  execute:\n", 1)
        self.assertNotIn("JULES_API_KEY", preflight)
        self.assertIn("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}", execute)
        self.assertIn("permissions:\n      contents: write", execute)

    def test_existing_runtime_gates_are_reused_not_reimplemented(self):
        self.assertIn("python -m ues.rp_authority_runtime", self.text)
        self.assertIn("python -m ues.lifecycle_runtime_observed", self.text)
        self.assertIn("python -m ues.initial_lineage_runtime", self.text)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON", self.text)
        self.assertIn("UES_AUTHORITY_TRANSPORT_ACTOR", self.text)

    def test_only_validated_routing_metadata_crosses_job_outputs(self):
        preflight, _execute = self.text.split("\n  execute:\n", 1)
        self.assertIn("('project', value.get('project', ''))", preflight)
        self.assertIn("('request_id', value.get('request_id', ''))", preflight)
        self.assertIn("('wakeup_repository', wakeup.get('repository', ''))", preflight)
        self.assertNotIn("('authority_event_id'", preflight)
        self.assertNotIn("authority_event_id: ${{ steps.metadata.outputs.authority_event_id }}", self.text)

    def test_queue_is_transport_not_raw_comment_effect_ingress(self):
        self.assertNotIn("issue_comment:", self.text)
        self.assertNotIn("/ues ", self.text)
        self.assertIn("PARENT_CONTROLLER_CONTROL_QUEUE", self.text)

    def test_request_and_runtime_results_are_preserved_as_bounded_artifacts(self):
        self.assertIn("ues-parent-request-", self.text)
        self.assertIn("retention-days: 1", self.text)
        self.assertIn("ues-parent-result-", self.text)
        self.assertIn("retention-days: 7", self.text)


if __name__ == "__main__":
    unittest.main()
