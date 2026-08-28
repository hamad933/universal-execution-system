from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / ".github" / "workflows" / "validate.yml"
RP_READONLY = ROOT / ".github" / "workflows" / "ues-rp-readonly-runtime.yml"
RP_AUTHORITY = ROOT / ".github" / "workflows" / "ues-rp-authority-lifecycle.yml"
PORTFOLIO = ROOT / ".github" / "workflows" / "ues-bounded-existing-session.yml"


class U26ActivationHardeningWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = PARENT.read_text(encoding="utf-8")
        cls.rp_readonly = RP_READONLY.read_text(encoding="utf-8")
        cls.rp_authority = RP_AUTHORITY.read_text(encoding="utf-8")
        cls.portfolio = PORTFOLIO.read_text(encoding="utf-8")

    def test_effect_writers_share_project_namespace_while_rp_maintenance_is_isolated(self):
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}",
            self.parent,
        )
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.rp_authority)
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.portfolio)
        self.assertNotIn("group: ues-project-lifecycle-${{ matrix.project }}", self.rp_readonly)
        self.assertIn("group: ues-rp-readonly-lifecycle-${{ matrix.project }}", self.rp_readonly)
        self.assertIn("group: ues-rp-provider-observer", self.rp_readonly)
        for text in (self.parent, self.rp_readonly, self.rp_authority, self.portfolio):
            self.assertIn("cancel-in-progress: false", text)

    def test_parent_control_heads_do_not_cancel_inflight_effect_runs(self):
        top_level = self.parent.split("\njobs:\n", 1)[0]
        self.assertIn(
            "group: validate-${{ github.workflow }}-${{ github.event.pull_request.head.ref == 'ues-parent-control' && github.event.pull_request.head.sha || (github.event.pull_request.number || github.ref) }}",
            top_level,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event.pull_request.head.ref != 'ues-parent-control' }}",
            top_level,
        )
        self.assertNotIn("cancel-in-progress: true", top_level)

    def test_rp_matrix_burst_allows_all_four_independent_projects(self):
        self.assertIn("max-parallel: 4", self.rp_readonly)
        self.assertIn("max-parallel: 4", self.rp_authority)
        self.assertNotIn("max-parallel: 2", self.rp_readonly)
        self.assertNotIn("max-parallel: 2", self.rp_authority)

    def test_parent_pipeline_preserves_sanitized_durable_receipt_artifact(self):
        self.assertIn("issues: write", self.parent)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.parent)
        self.assertIn("Preserve durable Parent Controller receipt evidence", self.parent)
        self.assertIn("parent-controller-receipt.md", self.parent)
        self.assertIn("if-no-files-found: error", self.parent)
        self.assertIn("Publish Parent Controller receipt comment (best effort)", self.parent)
        self.assertIn("continue-on-error: true", self.parent)
        self.assertIn(
            "CONTROL_PR_NUMBER: ${{ needs.parent-controller-preflight.outputs.control_pr_number }}",
            self.parent,
        )
        self.assertIn("issue_number: prNumber", self.parent)
        self.assertIn("external_effects_dispatched", self.parent)
        self.assertIn("new_tasks_or_sessions_created", self.parent)
        self.assertIn("effect_evidence_complete", self.parent)
        self.assertIn("'safe_to_blind_retry': False", self.parent)
        self.assertIn("'raw_session_ids_persisted': False", self.parent)
        self.assertIn("'secret_material_persisted': False", self.parent)

    def test_receipt_does_not_render_raw_current_authority(self):
        receipt_section = self.parent.split("Render sanitized durable Parent Controller receipt", 1)[1]
        self.assertNotIn("current-authority.json').read_text", receipt_section)
        self.assertNotIn("current_authority", receipt_section)
        self.assertNotIn("JULES_API_KEY", receipt_section)

    def test_parent_preflight_is_read_only_and_not_authority(self):
        preflight = self.parent.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertIn("contents: read", preflight)
        self.assertIn("issues: read", preflight)
        self.assertIn("pull-requests: read", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertNotIn("JULES_API_KEY", preflight)
        self.assertIn("python -m ues.parent_controller_request", preflight)

    def test_runtime_drift_check_still_precedes_secret_effect_step(self):
        execute = self.parent.split("\n  parent-controller-execute:\n", 1)[1]
        drift = execute.index("Reverify validated runtime is still current before effects")
        effect = execute.index("Run authority-gated lifecycle and guarded initial-lineage runtime")
        secret = execute.index("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}")
        self.assertLess(drift, effect)
        self.assertLess(drift, secret)


if __name__ == "__main__":
    unittest.main()
