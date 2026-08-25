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
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        verify = preflight.split("Verify exact control Git objects and preserve semantic request", 1)[1].split(
            "\n      - name: Verify Owner control commit identity", 1
        )[0]
        section = preflight.split("Rematerialize exact control request after trusted checkout", 1)[1].split(
            "\n      - name: Setup Python", 1
        )[0]

        self.assertIn("CONTROL_HEAD: ${{ github.event.pull_request.head.sha }}", verify)
        self.assertIn('git rev-parse "$CONTROL_HEAD:$REQUEST_PATH"', verify)
        self.assertIn('git hash-object "$RUNNER_TEMP/parent-controller-request.json"', verify)
        self.assertIn("EXPECTED_REQUEST_FILE_SHA: ${{ steps.control.outputs.request_file_sha }}", section)
        self.assertIn('$RUNNER_TEMP/parent-controller-request.json', section)
        self.assertIn('git hash-object "$saved"', section)
        self.assertIn("Parent Controller request changed across trusted runtime checkout", section)
        self.assertIn("install -m 600", section)
        self.assertIn("parent-controller-request.json", section)
        self.assertNotIn("github.rest.repos.getContent", section)

    def test_rematerialization_remains_read_only(self):
        preflight = self.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        self.assertIn("contents: read", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertNotIn("JULES_API_KEY", preflight)


if __name__ == "__main__":
    unittest.main()
