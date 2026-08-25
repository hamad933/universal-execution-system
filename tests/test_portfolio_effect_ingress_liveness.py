from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/ues-bounded-existing-session.yml")


class PortfolioEffectIngressLivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.fallback_job = cls.text.split("\n  provider-observer-fallback:\n", 1)[1].split("\n  project-lineage-cycle:\n", 1)[0]
        cls.effect_job = cls.text.split("\n  project-lineage-cycle:\n", 1)[1]
        cls.pre_matrix = cls.effect_job.split("\n    strategy:\n", 1)[0]

    def test_cep_gs_effect_ingress_is_explicit_event_driven_only(self) -> None:
        self.assertIn("github.event_name == 'workflow_dispatch'", self.pre_matrix)
        self.assertIn("github.event_name == 'repository_dispatch'", self.pre_matrix)
        self.assertNotIn("github.event_name == 'schedule'", self.pre_matrix)
        self.assertNotIn("github.event_name == 'push'", self.pre_matrix)
        self.assertNotIn("matrix.project", self.pre_matrix)

    def test_schedule_and_push_are_recovery_only(self) -> None:
        triggers = self.text.split("\npermissions:\n", 1)[0]
        self.assertIn("\n  push:\n", triggers)
        self.assertIn("\n  schedule:\n", triggers)
        self.assertIn("provider-observer-fallback:", self.text)
        self.assertIn("run: python -m ues.provider_observer_recovery", self.fallback_job)
        self.assertIn("github.event_name == 'schedule'", self.fallback_job)
        self.assertIn("github.event_name == 'push'", self.fallback_job)
        self.assertNotIn("workflow_dispatch", self.fallback_job)
        self.assertNotIn("repository_dispatch", self.fallback_job)

    def test_cep_and_gs_retain_full_cross_project_parallelism_and_bounded_dynamic_selection(self) -> None:
        self.assertIn("max-parallel: 2", self.effect_job)
        self.assertIn("[\"ALL\",\"CEP\",\"GS\"]", self.effect_job)
        self.assertIn("[\"CEP\",\"GS\"]", self.effect_job)
        self.assertIn("project: ${{ fromJSON(", self.effect_job)
        self.assertIn("group: ues-project-lifecycle-${{ matrix.project }}", self.effect_job)
        self.assertIn("cancel-in-progress: false", self.effect_job)

    def test_effect_job_still_requires_current_authority_transport(self) -> None:
        self.assertIn("UES_CURRENT_AUTHORITY_JSON:", self.effect_job)
        self.assertIn("python -m ues.lifecycle_runtime_observed ${{ matrix.project }}", self.effect_job)
        self.assertIn("python -m ues.initial_lineage_runtime ${{ matrix.project }}", self.effect_job)


if __name__ == "__main__":
    unittest.main()
