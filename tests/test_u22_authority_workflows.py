from __future__ import annotations

from pathlib import Path
import unittest


class U22AuthorityWorkflowTests(unittest.TestCase):
    def test_existing_gs_cep_ingress_runs_initial_lineage_runtime_under_same_authority_transport(self):
        text = Path(".github/workflows/ues-bounded-existing-session.yml").read_text(encoding="utf-8")
        effect_job = text.split("\n  project-lineage-cycle:\n", 1)[1]
        pre_matrix = effect_job.split("\n    strategy:\n", 1)[0]

        self.assertIn("python -m ues.lifecycle_runtime_observed ${{ matrix.project }}", effect_job)
        self.assertIn("python -m ues.initial_lineage_runtime ${{ matrix.project }}", effect_job)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON:", effect_job)
        self.assertIn("UES_AUTHORITY_TRANSPORT_ACTOR: ${{ github.actor }}", effect_job)
        self.assertIn("max-parallel: 2", effect_job)
        self.assertIn('["ALL","CEP","GS"]', effect_job)
        self.assertIn('["CEP","GS"]', effect_job)
        self.assertIn("project: ${{ fromJSON(", effect_job)
        self.assertNotIn("matrix.project", pre_matrix)

    def test_rp_effect_ingress_is_explicit_current_authority_only(self):
        text = Path(".github/workflows/ues-rp-authority-lifecycle.yml").read_text(encoding="utf-8")
        effect_job = text.split("\n  rp-current-authority-cycle:\n", 1)[1]
        pre_matrix = effect_job.split("\n    strategy:\n", 1)[0]

        self.assertIn("workflow_dispatch:", text)
        self.assertIn("repository_dispatch:", text)
        self.assertIn("types: [ues-rp-lifecycle-wakeup]", text)
        self.assertIn("current_authority_json:", text)
        self.assertIn("required: true", text)
        self.assertIn("max-parallel: 4", effect_job)
        self.assertIn('["ALL","RP01","RP02","RP03","RP04"]', effect_job)
        self.assertIn('["RP01","RP02","RP03","RP04"]', effect_job)
        self.assertIn("project: ${{ fromJSON(", effect_job)
        self.assertNotIn("matrix.project", pre_matrix)
        self.assertIn("python -m ues.rp_authority_runtime ${{ matrix.project }}", effect_job)
        self.assertIn("python -m ues.initial_lineage_retry_runtime ${{ matrix.project }}", effect_job)
        self.assertIn("UES_INITIAL_LINEAGE_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS:", effect_job)
        self.assertNotIn("\n  schedule:", text)
        self.assertNotIn("\n  push:", text)
        self.assertNotIn("pull_request_target", text)

    def test_rp_readonly_runtime_runs_on_main_push_but_remains_authority_neutral(self):
        text = Path(".github/workflows/ues-rp-readonly-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("\n  push:\n    branches:\n      - main", text)
        self.assertIn("\n  schedule:", text)
        self.assertIn('UES_CURRENT_AUTHORITY_JSON: ""', text)
        self.assertIn("project: [RP01, RP02, RP03, RP04]", text)
        self.assertNotIn("repository_dispatch", text)
        self.assertNotIn("create-initial-lineage-session", text)
        self.assertNotIn("create-session", text.lower())
        self.assertNotIn("send-message", text.lower())


if __name__ == "__main__":
    unittest.main()
