from __future__ import annotations

import unittest
from unittest.mock import patch

from ues import exact_terminal_readback as target
from ues import terminal_recovery as recovery
from ues import terminal_results


class ExactTerminalReadbackTests(unittest.TestCase):
    def test_filtered_index_keeps_only_exact_workstream(self) -> None:
        def indexer(store, *, project, route):
            return {
                "fp-a": [
                    {"workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", "generation": 1},
                    {"workstream": "RP03-IPA-S04", "generation": 2},
                ],
                "fp-b": [{"workstream": "RP03-IPA-S07", "generation": 2}],
            }

        filtered = target._filtered_index(indexer, "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")
        self.assertEqual(
            filtered(None, project="RP03", route="RP03"),
            {"fp-a": [{"workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", "generation": 1}]},
        )

    def test_run_restores_global_hooks_and_suppresses_pending_identity_reconciliation(self) -> None:
        original_results = terminal_results.lineage_index
        original_recovery = recovery.lineage_index
        original_pending = recovery._pending_identity_candidates
        observed = {}

        def fake_run(projects):
            observed["projects"] = list(projects)
            observed["pending"] = recovery._pending_identity_candidates(None, [])
            observed["filtered"] = terminal_results.lineage_index(
                None, project="RP03", route="RP03"
            )
            return {"result": "TERMINAL_BACKFILL_COMPLETE", "external_effects_dispatched": 0}

        def fake_index(store, *, project, route):
            return {
                "fp-target": [{"workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT"}],
                "fp-other": [{"workstream": "RP03-IPA-S04"}],
            }

        with patch.object(terminal_results, "lineage_index", fake_index), patch.object(
            recovery, "lineage_index", fake_index
        ), patch.object(target.terminal_backfill, "run", fake_run):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")

        self.assertEqual(observed["projects"], ["RP03"])
        self.assertEqual(observed["pending"], {})
        self.assertEqual(list(observed["filtered"]), ["fp-target"])
        self.assertTrue(result["exact_workstream_readback"])
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertIs(terminal_results.lineage_index, original_results)
        self.assertIs(recovery.lineage_index, original_recovery)
        self.assertIs(recovery._pending_identity_candidates, original_pending)

    def test_validation_rejects_wrong_project_or_workstream(self) -> None:
        with self.assertRaises(ValueError):
            target.run("GS", "RP03-IPA-S02")
        with self.assertRaises(ValueError):
            target.run("RP03", "../bad")


if __name__ == "__main__":
    unittest.main()
