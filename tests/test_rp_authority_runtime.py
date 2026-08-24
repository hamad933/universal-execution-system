from __future__ import annotations

import unittest
from unittest.mock import patch

from ues import lifecycle_runtime as legacy
from ues.rp_authority_runtime import run


class RPAuthorityRuntimeTests(unittest.TestCase):
    def test_wrapper_only_supplies_rp_adapter_and_grants_no_authority(self):
        original_loader = legacy._load_adapter

        def fake_observed(project: str):
            adapter = legacy._load_adapter(project)
            self.assertEqual(adapter["project"], "RP02")
            self.assertEqual(adapter["repository"], "hamad933/Enterprise-Operations-Control")
            return {
                "project": project,
                "current_authority_loaded": True,
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
            }

        with patch("ues.rp_authority_runtime.observed.run", side_effect=fake_observed):
            result = run("RP02")

        self.assertIs(legacy._load_adapter, original_loader)
        self.assertEqual(result["project"], "RP02")
        self.assertEqual(result["rp_runtime_mode"], "CURRENT_AUTHORITY_GATED")
        self.assertFalse(result["runtime_wrapper_grants_authority"])

    def test_non_rp_project_is_rejected(self):
        with self.assertRaises(ValueError):
            run("GS")


if __name__ == "__main__":
    unittest.main()
