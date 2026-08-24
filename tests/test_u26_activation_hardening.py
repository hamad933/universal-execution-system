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

    def test_same_project_lifecycle_writers_share_one_concurrency_namespace(self):
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}",
            self.parent,
        )
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.rp_readonly)
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.rp_authority)
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.portfolio)
        for text in (self.parent, self.rp_readonly, self.rp_authority, self.portfolio):
            self.assertIn("cancel-in-progress: false", text)

    def test_rp_matrix_burst_is_bounded(self):
        self.assertIn("max-parallel: 2", self.rp_readonly)
        self.assertIn("max-parallel: 2", self.rp_authority)
        self.assertNotIn("max-parallel: 4", self.rp_readonly)
        self.assertNotIn("max-parallel: 4", self.rp_authority)

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
