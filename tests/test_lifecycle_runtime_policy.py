from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.lifecycle_runtime import _next_generation, _pr_branch_match
from ues.lineage_registry import session_fingerprint, upsert_lineage_observation
from ues.state_store import DeterministicFileStateStore


class LifecycleRuntimePolicyTests(unittest.TestCase):
    def test_pr_head_branch_is_checked_independently_from_provider_binding(self) -> None:
        state = {"pr": {"head_ref": "work/output", "head_sha": "a" * 40}}
        self.assertTrue(_pr_branch_match(state, {"pr_head_branch": "work/output"}))
        self.assertFalse(_pr_branch_match(state, {"pr_head_branch": "work/other"}))
        self.assertTrue(_pr_branch_match(state, {"starting_branch": "work/output"}))
        self.assertIsNone(_pr_branch_match(state, {}))

    def test_next_generation_is_computed_from_durable_lineage_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            self.assertEqual(_next_generation(store, "CEP", "PERSONAL:CEP", "W03", "WRITER"), 1)
            fp = session_fingerprint("sessions/current")
            upsert_lineage_observation(
                store,
                project="CEP",
                route="PERSONAL:CEP",
                workstream="W03",
                role="WRITER",
                binding={
                    "status": "PROVEN",
                    "reason": "EXACT",
                    "session_fingerprint": fp,
                    "provider_state": "COMPLETED",
                    "session": {
                        "_source_repository": "owner/repo",
                        "sourceStartingBranch": "main",
                    },
                },
                policy={"known_session_fingerprints": [fp]},
            )
            self.assertEqual(_next_generation(store, "CEP", "PERSONAL:CEP", "W03", "WRITER"), 2)


if __name__ == "__main__":
    unittest.main()
