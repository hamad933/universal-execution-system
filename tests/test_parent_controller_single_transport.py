from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / ".github" / "workflows" / "ues-parent-controller-control-queue.yml"
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"
RECEIVER = ROOT / ".github" / "workflows" / "ues-parent-controller-dispatch.yml"
MANUAL = ROOT / "docs" / "PARENT_CONTROLLER_OPERATOR_MANUAL_V2.md"


class ParentControllerSingleTransportTests(unittest.TestCase):
    def test_legacy_parent_controller_trigger_workflow_is_retired(self):
        self.assertFalse(LEGACY.exists())

    def test_current_transport_is_validate_to_trusted_dispatch(self):
        validate = VALIDATE.read_text(encoding="utf-8")
        receiver = RECEIVER.read_text(encoding="utf-8")
        self.assertIn("parent-controller-relay:", validate)
        self.assertIn("github.rest.actions.createWorkflowDispatch", validate)
        self.assertIn("workflow_id: 'ues-parent-controller-dispatch.yml'", validate)
        self.assertIn("workflow_dispatch:", receiver)
        trigger = receiver.split("permissions:", 1)[0]
        self.assertNotIn("pull_request_target:", trigger)
        self.assertNotIn("issue_comment:", trigger)
        self.assertNotIn("workflow_run:", trigger)

    def test_current_manual_describes_one_path_and_no_manual_comment(self):
        text = MANUAL.read_text(encoding="utf-8")
        self.assertIn("Status: CURRENT operator contract", text)
        self.assertIn("one automatic Parent Controller transport path", text)
        self.assertIn("Validate Universal Core exact-head core PASS", text)
        self.assertIn("secretless trusted dispatch relay", text)
        self.assertIn("default-branch UES Parent Controller Trusted Dispatch", text)
        self.assertIn("no manual comment is required", text)
        self.assertIn("OPEN / DRAFT / DO_NOT_MERGE", text)


if __name__ == "__main__":
    unittest.main()
