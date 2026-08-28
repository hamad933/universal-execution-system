from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / ".github" / "workflows" / "ues-parent-controller-control-queue.yml"
DISPATCH = ROOT / ".github" / "workflows" / "ues-parent-controller-dispatch.yml"
VALIDATE = ROOT / ".github" / "workflows" / "validate.yml"
MANUAL = ROOT / "docs" / "PARENT_CONTROLLER_OPERATOR_MANUAL_V2.md"


class ParentControllerSingleTransportTests(unittest.TestCase):
    def test_superseded_parent_controller_transport_workflows_are_retired(self):
        self.assertFalse(LEGACY.exists())
        self.assertFalse(DISPATCH.exists())

    def test_current_transport_is_one_inline_validated_pipeline(self):
        text = VALIDATE.read_text(encoding="utf-8")
        self.assertIn("parent-controller-preflight:", text)
        self.assertIn("parent-controller-execute:", text)
        self.assertNotIn("parent-controller-relay:", text)
        self.assertNotIn("createWorkflowDispatch", text)
        self.assertNotIn("workflow_dispatch", text)
        self.assertIn("needs: [core, parent-controller-preflight]", text)
        self.assertIn("needs.core.result == 'success'", text)

    def test_current_manual_describes_one_path_and_no_manual_handoff(self):
        text = MANUAL.read_text(encoding="utf-8")
        self.assertIn("Status: CURRENT operator contract", text)
        self.assertIn("one automatic Parent Controller transport path", text)
        self.assertIn("Validate Universal Core exact-head core PASS", text)
        self.assertIn("read-only Parent Controller preflight", text)
        self.assertIn("authority-gated execute job", text)
        self.assertIn("no manual comment", text)
        self.assertIn("no workflow dispatch", text)
        self.assertIn("OPEN / DRAFT / DO NOT MERGE", text)


if __name__ == "__main__":
    unittest.main()
