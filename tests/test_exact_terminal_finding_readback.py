from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ues import exact_terminal_finding_readback as target
from ues.identity import canonical_lane_id


class _Store:
    def __init__(self, evidence, *, success_role="ASSURANCE"):
        self.evidence = evidence
        self.success_role = success_role
        self.read_count = 0
        self.lane_ids = []

    def read_workstream(self, lane_id):
        self.read_count += 1
        self.lane_ids.append(lane_id)
        expected = canonical_lane_id(
            "RP03", "RP03", target._logical_lineage_key("RP03-IPA-S02-EVIDENCE-SUPPLEMENT", self.success_role)
        )
        if lane_id != expected:
            return SimpleNamespace(status="MISSING", record=None)
        record = SimpleNamespace(project="RP03", route="RP03", evidence_bindings=self.evidence)
        return SimpleNamespace(status="OK", record=record)


class ExactTerminalFindingReadbackTests(unittest.TestCase):
    def _stored(self):
        return {
            "project": "RP03",
            "route": "RP03",
            "logical_workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "role": "ASSURANCE",
            "generation": 1,
            "session_fingerprint": "a" * 64,
            "repository": "hamad933/BOOKING-SERVICES",
            "verdict": "UNKNOWN",
            "candidate_sha": "0" * 40,
            "reviewed_sha": "0" * 40,
            "finding_count": 1,
            "result_state": "PARENT_CONSUMABLE",
            "freshness_status": "FRESH",
            "findings": [
                {
                    "finding_id": "F-1",
                    "severity": "BLOCKER",
                    "path": "S02",
                    "locator": "visual comparison",
                    "summary": "Reference state could not be established.",
                    "recommended_action": "Provide canonical reference evidence.",
                    "evidence_references": ["digest:abc"],
                    "private_source_repository": "secret/private-repo",
                    "raw_activity": "must-not-leak",
                    "session_id": "sessions/raw-id",
                }
            ],
        }

    def test_reads_only_bounded_canonical_role_lanes_and_projects_allowlisted_findings(self) -> None:
        stored = self._stored()
        evidence = {
            "role": "ASSURANCE",
            "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "generation": 1,
            "session_fingerprint": "a" * 64,
            "current_candidate_sha": "0" * 40,
            target.recovery.TERMINAL_RESULT_KEY: stored,
        }
        store = _Store(evidence)
        with patch.object(target.recovery, "load_governed_projects", return_value=({"project": "RP03", "route": "RP03", "repository": "hamad933/BOOKING-SERVICES"},)):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", store=store)

        self.assertEqual(store.read_count, 3)
        self.assertEqual(
            store.lane_ids,
            [
                canonical_lane_id("RP03", "RP03", target._logical_lineage_key("RP03-IPA-S02-EVIDENCE-SUPPLEMENT", role))
                for role in target._ALLOWED_ROLES
            ],
        )
        self.assertEqual(result["match_count"], 1)
        self.assertTrue(result["durable_lane_direct_read"])
        self.assertTrue(result["canonical_lane_identity_used"])
        self.assertEqual(result["bounded_role_lane_reads"], 3)
        self.assertFalse(result["lane_discovery_performed"])
        self.assertFalse(result["project_wide_lifecycle_scan_performed"])
        finding = result["results"][0]["findings"][0]
        self.assertEqual(finding["finding_id"], "F-1")
        self.assertEqual(finding["summary"], "Reference state could not be established.")
        self.assertNotIn("private_source_repository", finding)
        self.assertNotIn("raw_activity", finding)
        self.assertNotIn("session_id", finding)
        self.assertNotIn("secret/private-repo", repr(result))
        self.assertFalse(result["provider_live_read_performed"])
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["external_effects_dispatched"], 0)

    def test_moved_reviewed_sha_fails_closed(self) -> None:
        stored = self._stored()
        evidence = {
            "role": "ASSURANCE",
            "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
            "generation": 1,
            "session_fingerprint": "a" * 64,
            "current_candidate_sha": "1" * 40,
            target.recovery.TERMINAL_RESULT_KEY: stored,
        }
        store = _Store(evidence)
        with patch.object(target.recovery, "load_governed_projects", return_value=({"project": "RP03", "route": "RP03", "repository": "hamad933/BOOKING-SERVICES"},)):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT", store=store)
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["results"], [])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_validation_rejects_invalid_scope(self) -> None:
        with self.assertRaises(ValueError):
            target.run("GS", "RP03-IPA-S02")
        with self.assertRaises(ValueError):
            target.run("RP03", "../bad")


if __name__ == "__main__":
    unittest.main()
