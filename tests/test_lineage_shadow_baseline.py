from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.lineage_registry import lineage_lane_id, session_fingerprint, upsert_lineage_observation
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord


class LineageShadowBaselineTests(unittest.TestCase):
    def test_passive_observation_repairs_stale_active_lane_without_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            lane_id = lineage_lane_id("CEP", "PERSONAL:CEP", "W03", "WRITER")
            stale = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project="CEP",
                route="PERSONAL:CEP",
                workstream_id="LINEAGE::W03::WRITER",
                activation_mode="ACTIVE_AUTO_SAFE",
                authority_provenance={"effect_scope_active": True},
            )
            store.compare_and_swap_workstream(lane_id, 0, stale)

            fp = session_fingerprint("sessions/existing")
            result = upsert_lineage_observation(
                store,
                project="CEP",
                route="PERSONAL:CEP",
                workstream="W03",
                role="WRITER",
                binding={
                    "status": "PROVEN",
                    "reason": "EXACT_GOVERNED_LINEAGE_BINDING",
                    "session_fingerprint": fp,
                    "provider_state": "IN_PROGRESS",
                    "session": {
                        "_source_repository": "owner/repo",
                        "sourceStartingBranch": "provider/base",
                    },
                },
                policy={"known_session_fingerprints": [fp]},
            )

            self.assertEqual(result["binding_status"], "PROVEN")
            read = store.read_workstream(lane_id)
            self.assertEqual(read.status, "OK")
            self.assertIsNotNone(read.record)
            assert read.record is not None
            self.assertEqual(read.record.activation_mode, "SHADOW")
            self.assertTrue(
                bool((read.record.authority_provenance or {}).get("observation_resets_transient_effect_authority"))
            )
            self.assertIsNone(read.record.action_in_flight)
            self.assertIsNone(read.record.unknown_write_state)


if __name__ == "__main__":
    unittest.main()
