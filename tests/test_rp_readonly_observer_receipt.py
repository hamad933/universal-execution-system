from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ues-rp-readonly-runtime.yml"


class RpReadonlyObserverReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.observer = cls.text.split("\n  provider-observer:\n", 1)[1].split(
            "\n  lifecycle-health:\n", 1
        )[0]

    def test_runtime_exit_status_is_preserved_after_receipt_materialization(self):
        self.assertIn("set +e", self.observer)
        self.assertIn("status=$?", self.observer)
        self.assertIn('exit "$status"', self.observer)

    def test_sanitized_receipt_is_uploaded_even_when_observer_fails(self):
        self.assertIn("Preserve sanitized RP observer receipt", self.observer)
        self.assertIn("if: always()", self.observer)
        self.assertIn("actions/upload-artifact@v4", self.observer)
        self.assertIn("rp-provider-observer-public-receipt.json", self.observer)
        self.assertIn("if-no-files-found: error", self.observer)

    def test_public_receipt_is_explicitly_zero_effect_and_private_content_free(self):
        self.assertIn('"provider_mutation_performed"', self.observer)
        self.assertIn('"new_tasks_or_sessions_created"', self.observer)
        self.assertIn('"external_effects_dispatched"', self.observer)
        self.assertIn('"safe_to_blind_retry"', self.observer)
        self.assertIn('"recovery_snapshot_persisted": False', self.observer)
        self.assertIn('"findings_persisted": False', self.observer)
        self.assertIn('"raw_activity_content_persisted": False', self.observer)
        self.assertIn('"raw_session_ids_persisted": False', self.observer)
        self.assertIn('"raw_titles_persisted": False', self.observer)
        self.assertIn('"secret_material_persisted": False', self.observer)
        self.assertNotIn('payload.get("sanitized_recovery_snapshot")', self.observer)
        self.assertNotIn('payload.get("findings")', self.observer)
        self.assertNotIn('payload.get("projects")', self.observer)


if __name__ == "__main__":
    unittest.main()
