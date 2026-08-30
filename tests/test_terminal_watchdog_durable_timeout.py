from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-terminal-result-backfill.yml"


class TerminalWatchdogDurableTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.watchdog = cls.text.split("  terminal-watchdog:\n", 1)[1]

    def test_internal_watchdog_deadline_precedes_outer_job_timeout(self) -> None:
        outer = re.search(r"^    timeout-minutes: (\d+)$", self.watchdog, re.MULTILINE)
        internal = re.search(
            r'UES_TERMINAL_WATCHDOG_WALL_CLOCK_BUDGET_SECONDS: "(\d+)"',
            self.watchdog,
        )
        self.assertIsNotNone(outer)
        self.assertIsNotNone(internal)
        self.assertGreater(int(outer.group(1)) * 60, int(internal.group(1)))
        self.assertGreaterEqual(int(outer.group(1)) * 60 - int(internal.group(1)), 120)

    def test_budget_exhaustion_is_machine_actionable_and_fail_closed(self) -> None:
        self.assertIn("TERMINAL_LIVENESS_WATCHDOG_BUDGET_EXHAUSTED", self.watchdog)
        self.assertIn('"scan_complete": False', self.watchdog)
        self.assertIn('"provider_mutation_performed": False', self.watchdog)
        self.assertIn('"external_effects_dispatched": 0', self.watchdog)
        self.assertIn('"new_tasks_or_sessions_created": 0', self.watchdog)
        self.assertIn('"safe_to_blind_retry": False', self.watchdog)
        self.assertIn('"blocked_lane_freezes_independent_lanes": False', self.watchdog)

    def test_watchdog_receipt_is_always_preserved(self) -> None:
        self.assertIn('receipt="$RUNNER_TEMP/ues-terminal-watchdog.json"', self.watchdog)
        self.assertIn("timeout --signal=TERM", self.watchdog)
        self.assertIn("- name: Preserve terminal watchdog receipt", self.watchdog)
        preserve = self.watchdog.split("- name: Preserve terminal watchdog receipt", 1)[1]
        self.assertIn("if: always()", preserve)
        self.assertIn("ues-terminal-watchdog-${{ github.run_id }}", preserve)
        self.assertIn("if-no-files-found: error", preserve)

    def test_parallel_project_backfill_contract_is_unchanged(self) -> None:
        self.assertIn("fail-fast: false", self.text)
        self.assertIn("max-parallel: 2", self.text)
        self.assertIn(
            "group: ues-terminal-result-backfill-${{ matrix.project }}\n      cancel-in-progress: false",
            self.text,
        )
        self.assertIn(
            "group: ues-terminal-result-watchdog\n      cancel-in-progress: false",
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
