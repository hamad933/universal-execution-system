from __future__ import annotations

import unittest

from ues.provider_observer import PROJECTS, observation_manifest


class Issue222RP02ProviderObservationRegressionTests(unittest.TestCase):
    def test_canonical_provider_observer_includes_governed_rp02_adapter_identity(self):
        projects = {item["project"]: item for item in PROJECTS}
        self.assertIn("RP02", projects)
        self.assertEqual(projects["RP02"]["route"], "RP02")
        self.assertEqual(
            projects["RP02"]["repository"],
            "hamad933/Enterprise-Operations-Control",
        )

    def test_manifest_exposes_a_deterministic_rp02_observation_lane(self):
        manifest = observation_manifest()
        projects = {item["project"]: item for item in manifest["projects"]}
        self.assertIn("RP02", projects)
        self.assertEqual(projects["RP02"]["route"], "RP02")
        self.assertEqual(
            projects["RP02"]["repository"],
            "hamad933/Enterprise-Operations-Control",
        )
        self.assertTrue(projects["RP02"]["lane_id"])
        self.assertTrue(projects["RP02"]["state_ref"].startswith("ues-runtime/v2/lane/"))


if __name__ == "__main__":
    unittest.main()
