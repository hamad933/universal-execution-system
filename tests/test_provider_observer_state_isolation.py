from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from ues import provider_observer_runtime as runtime
from ues.identity import canonical_lane_id
from ues.rp_readonly_runtime import RP_PROJECTS


class ProviderObserverStateIsolationTests(unittest.TestCase):
    def test_default_gs_cep_health_lane_identity_is_preserved(self):
        self.assertEqual(
            runtime._health_lane_id(),
            canonical_lane_id("UES", "INTERNAL:UES", "PROVIDER-OBSERVER-HEALTH"),
        )
        self.assertEqual(runtime._observer_project_scope(), ("CEP", "GS"))

    def test_rp_observer_uses_disjoint_health_lane(self):
        default_lane = runtime._health_lane_id()
        with patch.object(runtime, "PROJECTS", RP_PROJECTS):
            rp_lane = runtime._health_lane_id()
            self.assertEqual(
                runtime._health_workstream_id(),
                "PROVIDER-OBSERVER-HEALTH-RP01-RP02-RP03-RP04",
            )
            self.assertEqual(
                runtime._observer_project_scope(),
                ("RP01", "RP02", "RP03", "RP04"),
            )
        self.assertNotEqual(rp_lane, default_lane)

    def test_candidate_proof_never_writes_live_runtime_state_prefix(self):
        text = Path(".github/workflows/ues-live-runtime-foundation.yml").read_text(
            encoding="utf-8"
        )
        candidate = text.split("  candidate-proof:", 1)[1].split(
            "  scheduled-provider-observer:", 1
        )[0]
        live = text.split("  scheduled-provider-observer:", 1)[1]

        self.assertIn(
            "UES_STATE_REF_PREFIX: ues-runtime/candidate/${{ github.ref_name }}",
            candidate,
        )
        self.assertNotIn("UES_STATE_REF_PREFIX: ues-runtime/v2", candidate)
        self.assertIn("UES_STATE_REF_PREFIX: ues-runtime/v2", live)


if __name__ == "__main__":
    unittest.main()
