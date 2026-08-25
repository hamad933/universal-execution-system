from __future__ import annotations

from pathlib import Path
import unittest


class RPParallelWorkflowLivenessTests(unittest.TestCase):
    def test_rp_effect_ingress_is_event_driven_and_all_projects_can_run_together(self):
        text = Path(".github/workflows/ues-rp-authority-lifecycle.yml").read_text(encoding="utf-8")
        effect_job = text.split("\n  rp-current-authority-cycle:\n", 1)[1]
        pre_matrix = effect_job.split("\n    strategy:\n", 1)[0]

        self.assertIn("workflow_dispatch:", text)
        self.assertIn("repository_dispatch:", text)
        self.assertIn("types: [ues-rp-lifecycle-wakeup]", text)
        self.assertNotIn("\n  schedule:", text)
        self.assertNotIn("\n  push:", text)
        self.assertIn("max-parallel: 4", text)
        self.assertNotIn("matrix.project", pre_matrix)
        self.assertIn("[\"ALL\",\"RP01\",\"RP02\",\"RP03\",\"RP04\"]", effect_job)
        self.assertIn("[\"RP01\",\"RP02\",\"RP03\",\"RP04\"]", effect_job)
        self.assertIn("project: ${{ fromJSON(", effect_job)

    def test_readonly_maintenance_has_full_project_parallelism_and_separate_lanes(self):
        text = Path(".github/workflows/ues-rp-readonly-runtime.yml").read_text(encoding="utf-8")

        self.assertIn("max-parallel: 4", text)
        self.assertIn("group: ues-rp-provider-observer", text)
        self.assertIn("group: ues-rp-readonly-lifecycle-${{ matrix.project }}", text)
        self.assertNotIn("\nconcurrency:\n  group: ues-rp-readonly-runtime", text)
        self.assertNotIn("group: ues-project-lifecycle-${{ matrix.project }}", text)

    def test_effect_paths_share_only_the_project_effect_lane_not_maintenance_lane(self):
        authority = Path(".github/workflows/ues-rp-authority-lifecycle.yml").read_text(encoding="utf-8")
        parent = Path(".github/workflows/validate.yml").read_text(encoding="utf-8")
        readonly = Path(".github/workflows/ues-rp-readonly-runtime.yml").read_text(encoding="utf-8")

        effect_lane = "ues-project-lifecycle-${{ matrix.project }}"
        parent_effect_lane = "ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}"
        self.assertIn(effect_lane, authority)
        self.assertIn(parent_effect_lane, parent)
        self.assertNotIn("ues-rp-readonly-lifecycle-", authority)
        self.assertNotIn("ues-rp-readonly-lifecycle-", parent)
        self.assertNotIn(effect_lane, readonly)

    def test_terminal_backfill_remains_independent_recovery_lane(self):
        text = Path(".github/workflows/ues-terminal-result-backfill.yml").read_text(encoding="utf-8")

        # Terminal recovery stays project-scoped and concurrent, while the shared
        # account-global provider inventory is admitted at a bounded width.
        self.assertIn("max-parallel: 2", text)
        self.assertIn("group: ues-terminal-result-backfill-${{ matrix.project }}", text)
        self.assertIn("\n  schedule:\n", text)
        self.assertNotIn("group: ues-project-lifecycle-${{ matrix.project }}", text)


if __name__ == "__main__":
    unittest.main()
