from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-terminal-result-backfill.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


class TerminalBackfillWorkflowLivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _workflow_text()

    def test_project_lifecycle_is_in_same_matrix_lane_as_backfill(self) -> None:
        self.assertNotIn("  lifecycle-readback:\n", self.text)
        self.assertIn("Read this project lifecycle immediately after its backfill", self.text)
        self.assertIn("UES_LIFECYCLE_PROJECT: ${{ matrix.project }}", self.text)
        self.assertIn("group: ues-terminal-result-backfill-${{ matrix.project }}", self.text)

    def test_backfill_failure_does_not_suppress_same_project_lifecycle_readback(self) -> None:
        lifecycle = self.text.index("Read this project lifecycle immediately after its backfill")
        lifecycle_if = self.text.rfind("if: always()", 0, lifecycle)
        self.assertNotEqual(lifecycle_if, -1)
        receipt = self.text.index("Preserve this project lifecycle readback receipt", lifecycle)
        receipt_if = self.text.rfind("if: always()", lifecycle, receipt)
        self.assertNotEqual(receipt_if, -1)

    def test_project_pipeline_preserves_independent_phase_timeouts(self) -> None:
        backfill = self.text.index("Read-only project-scoped terminal backfill")
        lifecycle = self.text.index("Read this project lifecycle immediately after its backfill")
        self.assertIn("timeout-minutes: 70", self.text[:backfill])
        self.assertIn("timeout-minutes: 45", self.text[backfill:lifecycle])
        self.assertIn("timeout-minutes: 20", self.text[lifecycle:])

    def test_terminal_backfill_uses_current_hard_bounded_activity_workers(self) -> None:
        backfill = self.text.index("Read-only project-scoped terminal backfill")
        lifecycle = self.text.index("Read this project lifecycle immediately after its backfill")
        phase = self.text[backfill:lifecycle]
        self.assertIn('UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS: "4"', phase)

    def test_global_watchdog_waits_only_for_project_pipeline_completion(self) -> None:
        watchdog = self.text.index("  terminal-watchdog:\n")
        watchdog_text = self.text[watchdog:]
        self.assertIn("needs: [discover-projects, backfill]", watchdog_text)
        self.assertIn("blocked_lane_freezes_independent_lanes", self.text)
        self.assertIn("fail-fast: false", self.text)
        self.assertIn("max-parallel: 2", self.text)

    def test_watchdog_deadline_covers_current_proven_runtime_envelope(self) -> None:
        watchdog_text = self.text.split("  terminal-watchdog:\n", 1)[1]
        match = re.search(r"^    timeout-minutes: (\d+)$", watchdog_text, re.MULTILINE)
        self.assertIsNotNone(match)
        timeout = int(match.group(1))
        self.assertGreaterEqual(timeout, 20)
        self.assertLessEqual(timeout, 30)

    def test_project_and_watchdog_concurrency_never_cancel_in_progress(self) -> None:
        self.assertIn(
            "group: ues-terminal-result-backfill-${{ matrix.project }}\n      cancel-in-progress: false",
            self.text,
        )
        self.assertIn(
            "group: ues-terminal-result-watchdog\n      cancel-in-progress: false",
            self.text,
        )
        self.assertIn('cron: "11,26,41,56 * * * *"', self.text)


if __name__ == "__main__":
    unittest.main()
