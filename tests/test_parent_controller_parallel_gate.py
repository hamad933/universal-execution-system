from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"


class ParentControllerParallelGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = VALIDATE.read_text(encoding="utf-8")
        cls.preflight = cls.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        cls.execute = cls.text.split("\n  parent-controller-execute:\n", 1)[1]

    def test_trusted_preflight_no_longer_waits_for_full_core(self) -> None:
        header = self.preflight.split("\n    steps:\n", 1)[0]
        self.assertNotIn("needs: core", header)
        self.assertIn("Parent Controller trusted preflight", header)

    def test_execute_requires_both_core_and_trusted_preflight(self) -> None:
        header = self.execute.split("\n    steps:\n", 1)[0]
        self.assertIn("needs: [core, parent-controller-preflight]", header)
        self.assertIn("needs.core.result == 'success'", header)
        self.assertIn("needs.parent-controller-preflight.outputs.rate_limit_deferred != 'true'", header)
        self.assertIn("needs.parent-controller-preflight.outputs.already_receipted != 'true'", header)

    def test_same_project_effect_serialization_remains(self) -> None:
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}",
            self.execute,
        )
        self.assertIn("cancel-in-progress: false", self.execute)

    def test_trust_and_runtime_freshness_gates_are_preserved(self) -> None:
        self.assertIn("github.rest.repos.getCommit", self.preflight)
        self.assertIn("OWNER_IDENTITY_READ_UNAVAILABLE", self.preflight)
        self.assertIn("python -m ues.parent_controller_request", self.preflight)
        drift = self.execute.index("Reverify validated runtime is still current before effects")
        effect = self.execute.index("Run authority-gated lifecycle and guarded initial-lineage runtime")
        self.assertLess(drift, effect)


if __name__ == "__main__":
    unittest.main()
