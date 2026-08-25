from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"


class ParentControllerOwnerIdentityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = VALIDATE.read_text(encoding="utf-8")
        cls.preflight = cls.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        cls.failure_job = cls.text.split("\n  parent-controller-preflight-failure:\n", 1)[1].split(
            "\n  parent-controller-execute:\n", 1
        )[0]

    def test_owner_identity_gate_remains_strict(self):
        self.assertIn("github.rest.repos.getCommit", self.preflight)
        self.assertIn("controlCommit.author.login !== context.repo.owner", self.preflight)
        self.assertIn("controlCommit.committer.login !== context.repo.owner", self.preflight)
        self.assertIn("OWNER_IDENTITY_MISMATCH", self.preflight)
        self.assertIn("OWNER_IDENTITY_SCOPE_MISMATCH", self.preflight)

    def test_rest_read_failure_is_distinguished_and_fail_closed(self):
        self.assertIn("OWNER_IDENTITY_READ_UNAVAILABLE", self.preflight)
        self.assertIn("Owner identity readback unavailable; fail closed before effects", self.preflight)
        self.assertIn("safe_to_blind_retry: false", self.preflight)
        self.assertIn("effect_job_reached: false", self.preflight)
        self.assertIn("external_effects_dispatched: 0", self.preflight)
        self.assertIn("new_tasks_or_sessions_created: 0", self.preflight)

    def test_failure_evidence_is_artifact_backed_not_comment_dependent(self):
        self.assertIn("parent-controller-owner-identity-failure.json", self.preflight)
        self.assertIn("Preserve Owner identity pre-effect failure evidence", self.preflight)
        self.assertIn("actions/upload-artifact@v4", self.preflight)
        self.assertIn("if-no-files-found: error", self.preflight)
        self.assertIn("continue-on-error: true", self.failure_job)
        self.assertIn("UI mirror", self.failure_job)

    def test_unclassified_step_failure_still_materializes_zero_effect_evidence(self):
        self.assertIn("OWNER_IDENTITY_STEP_FAILURE_UNCLASSIFIED", self.preflight)
        self.assertIn("Materialize Owner identity failure fallback", self.preflight)
        self.assertIn("raw_session_ids_persisted: false", self.preflight)
        self.assertIn("secret_material_persisted: false", self.preflight)


if __name__ == "__main__":
    unittest.main()
