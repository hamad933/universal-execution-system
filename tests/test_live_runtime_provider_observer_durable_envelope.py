from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-live-runtime-foundation.yml"


class LiveRuntimeProviderObserverDurableEnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def _job_block(self, name: str, next_name: str) -> str:
        pattern = rf"^  {re.escape(name)}:\n(.*?)(?=^  {re.escape(next_name)}:|\Z)"
        match = re.search(pattern, self.text, flags=re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, f"missing workflow job {name}")
        return match.group(0)

    def test_observer_internal_budget_precedes_outer_job_timeout(self) -> None:
        block = self._job_block("scheduled-provider-observer", "scheduled-lifecycle-health-fallback")
        outer = re.search(r"timeout-minutes:\s*(\d+)", block)
        inner = re.search(r"timeout --signal=TERM --kill-after=15s\s+(\d+)s", block)
        self.assertIsNotNone(outer)
        self.assertIsNotNone(inner)
        outer_seconds = int(outer.group(1)) * 60
        inner_seconds = int(inner.group(1))
        self.assertGreaterEqual(outer_seconds - inner_seconds, 120)
        self.assertIn("JULES_PROVIDER_OBSERVATION_BUDGET_EXHAUSTED", block)
        self.assertIn('"safe_to_blind_retry": False', block)
        self.assertIn('"provider_mutation_performed": False', block)
        self.assertIn('"external_effects_dispatched": 0', block)
        self.assertIn('"new_tasks_or_sessions_created": 0', block)

    def test_observer_receipt_is_preserved_before_failure_is_enforced(self) -> None:
        block = self._job_block("scheduled-provider-observer", "scheduled-lifecycle-health-fallback")
        self.assertIn("id: provider_observer", block)
        self.assertIn("continue-on-error: true", block)
        self.assertIn("- name: Preserve provider observer execution receipt", block)
        self.assertIn("if: always()", block)
        self.assertIn("uses: actions/upload-artifact@v4", block)
        self.assertIn("if-no-files-found: error", block)
        self.assertLess(
            block.index("Preserve provider observer execution receipt"),
            block.index("Enforce provider observer result"),
        )

    def test_authority_neutral_fallback_runs_only_when_observer_did_not_succeed(self) -> None:
        block = self._job_block("scheduled-lifecycle-health-fallback", "scheduled-provider-audit")
        self.assertIn(
            "if: always() && github.event_name == 'schedule' && needs.scheduled-provider-observer.result != 'success'",
            block,
        )
        self.assertIn("needs: scheduled-provider-observer", block)
        self.assertIn("fail-fast: false", block)
        self.assertIn("max-parallel: 2", block)
        self.assertIn("cancel-in-progress: false", block)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON: \"\"", block)

    def test_global_workflow_concurrency_remains_non_cancelling(self) -> None:
        prefix = self.text.split("jobs:", 1)[0]
        self.assertIn("cancel-in-progress: false", prefix)


if __name__ == "__main__":
    unittest.main()
