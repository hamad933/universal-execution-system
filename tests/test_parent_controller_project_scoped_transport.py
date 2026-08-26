from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"


class ParentControllerProjectScopedTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = VALIDATE.read_text(encoding="utf-8")
        cls.preflight = cls.text.split("\n  parent-controller-preflight:\n", 1)[1].split(
            "\n  parent-controller-preflight-failure:\n", 1
        )[0]
        cls.control = cls.preflight.split(
            "Verify exact control Git objects and preserve semantic request", 1
        )[1].split("\n      - name: Verify Owner control commit identity", 1)[0]
        cls.owner = cls.preflight.split("Verify Owner control commit identity", 1)[1].split(
            "\n      - name: Materialize Owner identity failure fallback", 1
        )[0]

    def test_project_scoped_slots_are_allowlisted(self) -> None:
        self.assertIn(".ues/parent-controller-requests", self.control)
        for project in ("GS", "CEP", "RP01", "RP02", "RP03", "RP04"):
            self.assertIn(project, self.control)
        self.assertIn("is_allowed_request_path", self.control)

    def test_pr_may_accumulate_only_allowlisted_transport_slots(self) -> None:
        self.assertIn('for path in "${pr_files[@]}"', self.control)
        self.assertIn('is_allowed_request_path "$path"', self.control)
        self.assertNotIn('"${#pr_files[@]}" -ne 1', self.control)
        self.assertIn('"${#commit_files[@]}" -ne 1', self.control)
        self.assertIn('REQUEST_PATH="${commit_files[0]}"', self.control)

    def test_selected_slot_is_exact_blob_bound_and_exported(self) -> None:
        self.assertIn('git rev-parse "$CONTROL_HEAD:$REQUEST_PATH"', self.control)
        self.assertIn('git hash-object "$RUNNER_TEMP/parent-controller-request.json"', self.control)
        self.assertIn('echo "request_path=$REQUEST_PATH" >> "$GITHUB_OUTPUT"', self.control)
        self.assertIn("request_path: ${{ steps.control.outputs.request_path }}", self.preflight)

    def test_owner_identity_uses_exact_selected_slot(self) -> None:
        self.assertIn("REQUEST_PATH: ${{ steps.control.outputs.request_path }}", self.owner)
        self.assertIn("process.env.REQUEST_PATH", self.owner)
        self.assertIn("changed[0].filename !== requestPath", self.owner)

    def test_validated_payload_project_is_bound_to_project_slot(self) -> None:
        self.assertIn("Bind validated project to selected request slot", self.preflight)
        self.assertIn("transport_request_path", self.preflight)
        self.assertIn("project_scoped_transport_slot", self.preflight)
        self.assertIn("Parent Controller request project does not match project-scoped transport slot", self.preflight)

    def test_same_project_effect_serialization_is_preserved(self) -> None:
        execute = self.text.split("\n  parent-controller-execute:\n", 1)[1]
        self.assertIn(
            "group: ues-project-lifecycle-${{ needs.parent-controller-preflight.outputs.project }}",
            execute,
        )
        self.assertIn("cancel-in-progress: false", execute)


if __name__ == "__main__":
    unittest.main()
