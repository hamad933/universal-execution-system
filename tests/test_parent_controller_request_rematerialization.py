from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "validate.yml"


class ParentControllerRequestRematerializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_request_is_materialized_after_trusted_runtime_checkout(self):
        checkout = self.text.index("Checkout exact trusted UES runtime")
        materialize = self.text.index("Rematerialize exact control request after trusted checkout")
        validate = self.text.index("Validate semantic request against exact live runtime")
        self.assertLess(checkout, materialize)
        self.assertLess(materialize, validate)

    def test_rematerialization_is_exact_head_and_blob_bound(self):
        section = self.text.split("Rematerialize exact control request after trusted checkout", 1)[1].split(
            "\n      - name: Setup Python", 1
        )[0]
        self.assertIn("CONTROL_HEAD: ${{ github.event.pull_request.head.sha }}", section)
        self.assertIn("EXPECTED_REQUEST_FILE_SHA: ${{ steps.control.outputs.request_file_sha }}", section)
        self.assertIn("ref: exactHead", section)
        self.assertIn("String(requestFile.sha || '') !== expectedBlob", section)
        self.assertIn("Parent Controller request changed across trusted runtime checkout", section)
        self.assertIn("fs.writeFileSync", section)
        self.assertIn("parent-controller-request.json", section)

    def test_rematerialization_remains_read_only(self):
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertIn("contents: read", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertNotIn("JULES_API_KEY", preflight)


if __name__ == "__main__":
    unittest.main()
