from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-rp-readonly-runtime.yml"


class RpReadonlyObserverDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.observer = cls.text.split("\n  provider-observer:\n", 1)[1].split(
            "\n  lifecycle-health:\n", 1
        )[0]
        cls.lifecycle = cls.text.split("\n  lifecycle-health:\n", 1)[1].split(
            "\n  provider-audit:\n", 1
        )[0]

    def test_observer_has_inner_deadline_below_outer_job_envelope(self) -> None:
        outer = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", self.observer, re.MULTILINE)
        inner = re.search(r'UES_PROVIDER_OBSERVER_INNER_TIMEOUT:\s*"(\d+)m"', self.observer)
        self.assertIsNotNone(outer)
        self.assertIsNotNone(inner)
        self.assertLess(int(inner.group(1)), int(outer.group(1)))
        self.assertGreaterEqual(int(outer.group(1)) - int(inner.group(1)), 5)
        self.assertIn("timeout --signal=TERM --kill-after=15s", self.observer)

    def test_inner_deadline_materializes_explicit_fail_closed_receipt(self) -> None:
        self.assertIn('export UES_OBSERVER_COMMAND_STATUS="$status"', self.observer)
        self.assertIn("command_status == 124", self.observer)
        self.assertIn('"RUNTIME_INTERNAL_DEADLINE_EXCEEDED"', self.observer)
        self.assertIn('"PROVIDER_OBSERVER_INTERNAL_DEADLINE_EXCEEDED"', self.observer)
        self.assertIn('"provider_mutation_performed": bool(payload.get("provider_mutation_performed", False))', self.observer)
        self.assertIn('"safe_to_blind_retry": bool(payload.get("safe_to_blind_retry", False))', self.observer)
        self.assertIn('exit "$status"', self.observer)

    def test_lifecycle_health_is_not_suppressed_by_observer_lane_failure(self) -> None:
        self.assertIn("    if: always()", self.lifecycle)
        self.assertIn("    needs: provider-observer", self.lifecycle)
        self.assertIn("max-parallel: 4", self.lifecycle)
        self.assertIn("UES_CURRENT_AUTHORITY_JSON: \"\"", self.lifecycle)


if __name__ == "__main__":
    unittest.main()
