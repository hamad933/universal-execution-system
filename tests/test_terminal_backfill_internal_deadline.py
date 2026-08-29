from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from ues import terminal_backfill


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-terminal-result-backfill.yml"


class TerminalBackfillInternalDeadlineTests(unittest.TestCase):
    def test_default_internal_budget_finishes_before_actions_hard_kill(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UES_TERMINAL_BACKFILL_WALL_CLOCK_BUDGET_SECONDS", None)
            budget = terminal_backfill._backfill_wall_clock_budget_seconds()
        self.assertGreaterEqual(budget, 30 * 60)
        self.assertLessEqual(budget, 40 * 60)
        self.assertLess(budget, 45 * 60)

    def test_internal_budget_is_bounded_even_when_env_requests_too_much(self) -> None:
        with patch.dict(
            os.environ,
            {"UES_TERMINAL_BACKFILL_WALL_CLOCK_BUDGET_SECONDS": str(24 * 60 * 60)},
            clear=False,
        ):
            self.assertLessEqual(terminal_backfill._backfill_wall_clock_budget_seconds(), 40 * 60)

    def test_budget_exhausted_receipt_is_fail_closed_and_non_mutating(self) -> None:
        receipt = terminal_backfill._budget_exhausted_result(
            phase="activity_prefetch",
            completed_phases=["state_store", "lineage_index", "provider_inventory"],
            elapsed_seconds=2399.5,
        )
        self.assertEqual(receipt["result"], "TERMINAL_BACKFILL_BUDGET_EXHAUSTED")
        self.assertEqual(receipt["budget_exhausted_phase"], "activity_prefetch")
        self.assertEqual(
            receipt["completed_phases"],
            ["state_store", "lineage_index", "provider_inventory"],
        )
        self.assertFalse(receipt["provider_mutation_performed"])
        self.assertEqual(receipt["external_effects_dispatched"], 0)
        self.assertEqual(receipt["new_tasks_or_sessions_created"], 0)
        self.assertFalse(receipt["safe_to_blind_retry"])
        self.assertFalse(receipt["state_persistence_complete"])

    def test_workflow_budget_is_explicitly_shorter_than_step_timeout(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        backfill = text.index("Read-only project-scoped terminal backfill")
        lifecycle = text.index("Read this project lifecycle immediately after its backfill")
        phase = text[backfill:lifecycle]
        self.assertIn('UES_TERMINAL_BACKFILL_WALL_CLOCK_BUDGET_SECONDS: "2400"', phase)
        self.assertIn("timeout-minutes: 45", phase)


if __name__ == "__main__":
    unittest.main()
