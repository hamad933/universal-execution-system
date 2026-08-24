from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "validate.yml"


class ParentControllerReceiptArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.execute = cls.text.split("\n  parent-controller-execute:\n", 1)[1]

    def test_receipt_is_rendered_before_required_result_artifact(self):
        render = self.execute.index("Render sanitized durable Parent Controller receipt")
        artifact = self.execute.index("Preserve durable Parent Controller receipt evidence")
        self.assertLess(render, artifact)
        self.assertIn("parent-controller-receipt.md", self.execute)
        self.assertIn("if-no-files-found: error", self.execute)
        self.assertIn("retention-days: 7", self.execute)

    def test_result_artifact_contains_complete_compact_evidence(self):
        artifact = self.execute.split("Preserve durable Parent Controller receipt evidence", 1)[1].split(
            "Publish Parent Controller receipt comment", 1
        )[0]
        for path in (
            "parent-request-metadata.json",
            "lifecycle-result.json",
            "initial-lineage-result.json",
            "parent-controller-receipt.md",
        ):
            self.assertIn(path, artifact)

    def test_pr_comment_is_best_effort_projection_only(self):
        comment = self.execute.split("Publish Parent Controller receipt comment (best effort)", 1)[1]
        self.assertIn("continue-on-error: true", comment)
        self.assertIn("github.rest.issues.createComment", comment)
        self.assertIn("parent-controller-receipt.md", comment)

    def test_comment_projection_cannot_precede_or_replace_artifact(self):
        artifact = self.execute.index("Preserve durable Parent Controller receipt evidence")
        comment = self.execute.index("Publish Parent Controller receipt comment (best effort)")
        self.assertLess(artifact, comment)


if __name__ == "__main__":
    unittest.main()
