from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues import exact_terminal_readback as target
from ues import terminal_recovery as recovery
from ues import terminal_results
from ues.jules_source_probe import repository_fingerprint
from ues.lineage_registry import session_fingerprint


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

    def test_alias_bound_lineage_requires_exact_durable_hash_alias(self) -> None:
        fp = "a" * 64
        alias = "sha256:" + "b" * 64
        lane = "ues-lane:v1|RP03|RP03|LINEAGE"
        record = SimpleNamespace(
            evidence_bindings={
                "session_fingerprint": fp,
                "source_repository": alias,
                "current_candidate_sha": "0" * 40,
            }
        )

        class Store:
            def read_workstream(self, lane_id):
                self.lane_id = lane_id
                return SimpleNamespace(status="OK", record=record)

        with patch.object(
            terminal_results,
            "lineage_index",
            return_value={
                fp: [
                    {
                        "lane_id": lane,
                        "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
                        "role": "ASSURANCE",
                        "generation": 1,
                    }
                ]
            },
        ):
            binding = target._alias_bound_lineage(
                Store(),
                project="RP03",
                route="RP03",
                workstream="RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            )

        self.assertIsNotNone(binding)
        self.assertEqual(binding["fingerprint"], fp)
        self.assertEqual(binding["source_alias"], alias)
        self.assertEqual(binding["candidate_sha"], "0" * 40)

    def test_private_transport_source_is_verified_only_by_hash_alias(self) -> None:
        raw_repository = "Example/Private-Evidence"
        alias = "sha256:" + repository_fingerprint(raw_repository)
        session = {"sourceIdentifier": "sources/private-id"}
        sources = [{"name": "sources/private-id", "repository": raw_repository}]

        self.assertTrue(
            target._verified_transport_source(session, sources, expected_alias=alias)
        )
        self.assertFalse(
            target._verified_transport_source(
                session,
                sources,
                expected_alias="sha256:" + "0" * 64,
            )
        )

    def test_alias_exact_readback_logicalizes_to_product_repo_after_hash_proof(self) -> None:
        raw_repository = "Example/Private-Evidence"
        source_alias = "sha256:" + repository_fingerprint(raw_repository)
        session_name = "sessions/s02-private"
        fp = session_fingerprint(session_name)
        captured = {}

        class Client:
            def list_sources(self, *, page_size=100):
                return [{"name": "sources/private-id", "repository": raw_repository}]

            def list_sessions(self, *, page_size=100):
                return [
                    {
                        "name": session_name,
                        "normalizedState": "COMPLETED",
                        "sourceIdentifier": "sources/private-id",
                    }
                ]

            def list_activities(self, session, *, page_size=100):
                self.session = session
                return [{"agentMessaged": {"agentMessage": "structured-runtime-only"}}]

        candidate = {
            "structured": True,
            "role": "ASSURANCE",
            "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "status": "COMPLETE",
            "verdict": "PASS",
            "candidate_sha": "0" * 40,
            "reviewed_sha": "0" * 40,
            "context_state": "OK",
            "finding_count": 0,
            "findings": [],
        }
        materialized_result = {
            "logical_workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "role": "ASSURANCE",
            "generation": 1,
            "session_fingerprint": fp,
            "repository": "hamad933/BOOKING-SERVICES",
            "result_state": "PARENT_CONSUMABLE",
            "freshness_status": "FRESH",
            "verdict": "PASS",
            "finding_count": 0,
        }

        def fake_materialize(snapshot, store):
            captured["snapshot"] = snapshot
            return {"results": [materialized_result]}

        binding = {
            "fingerprint": fp,
            "source_alias": source_alias,
            "lineage": {
                "lane_id": "lane",
                "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
                "role": "ASSURANCE",
                "generation": 1,
            },
        }
        project = {
            "project": "RP03",
            "route": "RP03",
            "repository": "hamad933/BOOKING-SERVICES",
        }

        with patch.object(
            recovery,
            "extract_terminal_candidate_with_legacy_recovery",
            return_value=candidate,
        ), patch.object(
            terminal_results,
            "materialize_project_results",
            side_effect=fake_materialize,
        ), patch.object(
            recovery,
            "persist_terminal_result",
            return_value={
                "state": "TERMINAL_RESULT_PERSISTED",
                "authoritative_readback": True,
            },
        ):
            result = target._alias_exact_readback(
                project,
                binding,
                store=object(),
                client=Client(),
            )

        logical_session = captured["snapshot"]["sessions"][0]
        self.assertEqual(logical_session["source_repository"], "hamad933/BOOKING-SERVICES")
        self.assertTrue(logical_session["source_binding_proven"])
        self.assertEqual(result["result"], "TERMINAL_BACKFILL_COMPLETE")
        self.assertEqual(result["parent_consumable_result_count"], 1)
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["private_source_identity_persisted"])
        self.assertNotIn(raw_repository, repr(result))

    def test_alias_exact_readback_fails_closed_on_source_alias_mismatch(self) -> None:
        session_name = "sessions/s02-private"
        fp = session_fingerprint(session_name)

        class Client:
            def list_sources(self, *, page_size=100):
                return [{"name": "sources/private-id", "repository": "Example/Other"}]

            def list_sessions(self, *, page_size=100):
                return [
                    {
                        "name": session_name,
                        "normalizedState": "COMPLETED",
                        "sourceIdentifier": "sources/private-id",
                    }
                ]

        result = target._alias_exact_readback(
            {
                "project": "RP03",
                "route": "RP03",
                "repository": "hamad933/BOOKING-SERVICES",
            },
            {
                "fingerprint": fp,
                "source_alias": "sha256:" + "0" * 64,
                "lineage": {"lane_id": "lane"},
            },
            store=object(),
            client=Client(),
        )
        self.assertEqual(result["result"], "EXACT_TERMINAL_TRANSPORT_SOURCE_ALIAS_MISMATCH")
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["private_source_identity_persisted"])

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

        with patch.object(target, "build_live_state_store", side_effect=RuntimeError("offline-test")), patch.object(
            terminal_results, "lineage_index", fake_index
        ), patch.object(
            recovery, "lineage_index", fake_index
        ), patch.object(target.terminal_backfill, "run", fake_run):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")

        self.assertEqual(observed["projects"], ["RP03"])
        self.assertEqual(observed["pending"], {})
        self.assertEqual(list(observed["filtered"]), ["fp-target"])
        self.assertTrue(result["exact_workstream_readback"])
        self.assertTrue(result["dual_repository_binding_supported"])
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
