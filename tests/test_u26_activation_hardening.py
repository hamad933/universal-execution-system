from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / ".github" / "workflows" / "ues-parent-controller-dispatch.yml"
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"
RP_READONLY = ROOT / ".github" / "workflows" / "ues-rp-readonly-runtime.yml"
RP_AUTHORITY = ROOT / ".github" / "workflows" / "ues-rp-authority-lifecycle.yml"
PORTFOLIO = ROOT / ".github" / "workflows" / "ues-bounded-existing-session.yml"


class U26ActivationHardeningWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = PARENT.read_text(encoding="utf-8")
        cls.validate = VALIDATE.read_text(encoding="utf-8")
        cls.rp_readonly = RP_READONLY.read_text(encoding="utf-8")
        cls.rp_authority = RP_AUTHORITY.read_text(encoding="utf-8")
        cls.portfolio = PORTFOLIO.read_text(encoding="utf-8")

    def test_same_project_lifecycle_writers_share_one_concurrency_namespace(self):
        self.assertIn("group: ues-project-lifecycle-${{ needs.preflight.outputs.project }}", self.parent)
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

    def test_parent_receiver_publishes_sanitized_durable_receipt(self):
        self.assertIn("issues: write", self.parent)
        self.assertIn("UES_PARENT_CONTROLLER_RECEIPT_V1", self.parent)
        self.assertIn("Publish durable Parent Controller receipt", self.parent)
        self.assertIn("CONTROL_PR_NUMBER: ${{ needs.preflight.outputs.control_pr_number }}", self.parent)
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
        self.assertNotIn("secrets.JULES_API_KEY", receipt_section)

    def test_validate_relay_is_secretless_same_run_gate_not_authority(self):
        relay = self.validate.split("\n  parent-controller-relay:\n", 1)[1].split("\n  parent-controller-receiver:\n", 1)[0]
        self.assertNotIn("actions: write", relay)
        self.assertNotIn("createWorkflowDispatch", relay)
        self.assertNotIn("JULES_API_KEY", relay)
        self.assertNotIn("UES_CURRENT_AUTHORITY_JSON", relay)
        self.assertNotIn("contents: write", relay)
        receiver_call = self.validate.split("\n  parent-controller-receiver:\n", 1)[1]
        self.assertIn("uses: ./.github/workflows/ues-parent-controller-dispatch.yml", receiver_call)
        self.assertIn("needs: [core, parent-controller-relay]", receiver_call)

    def test_runtime_drift_check_still_precedes_secret_effect_step(self):
        drift = self.parent.index("Reverify validated runtime is still current before effects")
        effect = self.parent.index("Run authority-gated lifecycle and guarded initial-lineage runtime")
        self.assertLess(drift, effect)
        pre_effect = self.parent[:effect]
        self.assertNotIn("secrets.JULES_API_KEY", pre_effect)
        execute = self.parent.split("\n  execute:\n", 1)[1]
        self.assertIn("JULES_API_KEY: ${{ secrets.JULES_API_KEY }}", execute)


if __name__ == "__main__":
    unittest.main()
