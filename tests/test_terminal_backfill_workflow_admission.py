from __future__ import annotations

import unittest
from pathlib import Path


class TerminalBackfillWorkflowAdmissionTests(unittest.TestCase):
    def test_provider_facing_matrix_and_activity_parallelism_are_bounded(self):
        workflow = Path(".github/workflows/ues-terminal-result-backfill.yml").read_text(encoding="utf-8")

        self.assertIn("max-parallel: 2", workflow)
        self.assertIn('UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS: "4"', workflow)
        self.assertNotIn("max-parallel: 6", workflow)
        self.assertNotIn('UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS: "8"', workflow)


if __name__ == "__main__":
    unittest.main()
