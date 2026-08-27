from __future__ import annotations

import unittest
from unittest.mock import patch

from ues import exact_terminal_finding_readback as target


class ExactTerminalFindingReadbackTests(unittest.TestCase):
    def test_returns_only_exact_workstream_and_allowlisted_finding_fields(self) -> None:
        lifecycle = {
            "results": [
                {
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
                },
                {
                    "logical_workstream": "RP03-IPA-S04",
                    "findings": [{"summary": "unrelated"}],
                },
            ]
        }
        with patch.object(target.terminal_lifecycle, "run", return_value=lifecycle):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")

        self.assertEqual(result["match_count"], 1)
        self.assertEqual(len(result["results"]), 1)
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
        self.assertFalse(result["private_source_identity_persisted"])

    def test_missing_exact_result_fails_closed_without_mutation(self) -> None:
        with patch.object(target.terminal_lifecycle, "run", return_value={"results": []}):
            result = target.run("RP03", "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["results"], [])
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_validation_rejects_invalid_scope(self) -> None:
        with self.assertRaises(ValueError):
            target.run("GS", "RP03-IPA-S02")
        with self.assertRaises(ValueError):
            target.run("RP03", "../bad")


if __name__ == "__main__":
    unittest.main()
