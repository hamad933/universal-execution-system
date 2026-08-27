from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-targeted-project-terminal-readback.yml"


class TargetedTerminalReadbackConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_exact_workstream_uses_lane_specific_concurrency_group(self) -> None:
        self.assertIn(
            "format('ues-terminal-exact-readback-{0}-{1}', needs.authorize.outputs.project, needs.authorize.outputs.workstream)",
            self.text,
        )

    def test_whole_project_falls_back_to_canonical_project_group(self) -> None:
        self.assertIn(
            "format('ues-terminal-result-backfill-{0}', needs.authorize.outputs.project)",
            self.text,
        )

    def test_targeted_readback_never_cancels_in_progress_work(self) -> None:
        self.assertIn("cancel-in-progress: false", self.text)

    def test_exact_path_remains_get_only_and_does_not_generate_sessions(self) -> None:
        self.assertIn('python -m ues.exact_terminal_readback "$PROJECT" "$WORKSTREAM"', self.text)
        self.assertNotIn("create-session-generation", self.text)
        self.assertNotIn("jules.sessions.create", self.text)


if __name__ == "__main__":
    unittest.main()
