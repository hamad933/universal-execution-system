from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-bounded-existing-session.yml"


class ProviderObserverFallbackWorkflowEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")
        start = self.text.index("  provider-observer-fallback:")
        end = self.text.index("\n  project-lineage-cycle:", start)
        self.job = self.text[start:end]

    def test_fallback_envelope_covers_observed_runtime_without_cancel_semantics(self) -> None:
        match = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", self.job, re.MULTILINE)
        self.assertIsNotNone(match)
        timeout = int(match.group(1))
        self.assertGreaterEqual(timeout, 20)
        self.assertLessEqual(timeout, 30)
        self.assertIn("group: ues-provider-observer-fallback", self.job)
        self.assertIn("cancel-in-progress: false", self.job)

    def test_fallback_remains_read_only_recovery_entrypoint(self) -> None:
        self.assertIn("python -m ues.provider_observer_recovery", self.job)
        self.assertNotIn("lifecycle_runtime_observed", self.job)
        self.assertNotIn("initial_lineage_runtime", self.job)


if __name__ == "__main__":
    unittest.main()
