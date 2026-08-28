from __future__ import annotations

import unittest

from ues.terminal_lifecycle import _normalize_unstructured_reviewer_views


class TerminalLifecycleUnstructuredViewTests(unittest.TestCase):
    def test_missing_structured_reviewer_payload_is_not_reported_as_sha_mismatch(self):
        value = {
            "results": [{
                "role": "REVIEWER",
                "result_state": "REVIEWED_SHA_MISMATCH",
                "reviewed_sha": None,
                "verdict": None,
                "finding_count": None,
                "freshness_status": "UNBOUND",
                "parent_action_required": True,
            }],
            "summary": {
                "binding_counts": {"REVIEWED_SHA_MISMATCH": 1},
                "parent_consumable_result_count": 0,
                "terminal_result_count": 1,
                "terminal_unconsumed_result_count": 1,
            },
        }
        result = _normalize_unstructured_reviewer_views(value)
        self.assertEqual(
            result["results"][0]["result_state"],
            "COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION",
        )
        self.assertEqual(result["results"][0]["freshness_status"], "UNADJUDICABLE")
        self.assertTrue(result["results"][0]["safe_read_only_recovery_exists"])
        self.assertEqual(
            result["summary"]["binding_counts"],
            {"COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION": 1},
        )

    def test_explicit_wrong_reviewed_sha_remains_mismatch(self):
        value = {
            "results": [{
                "role": "REVIEWER",
                "result_state": "REVIEWED_SHA_MISMATCH",
                "reviewed_sha": "b" * 40,
                "verdict": "PASS",
                "finding_count": 0,
                "freshness_status": "STALE_AFTER_CANDIDATE_MOVEMENT",
            }],
            "summary": {},
        }
        result = _normalize_unstructured_reviewer_views(value)
        self.assertEqual(result["results"][0]["result_state"], "REVIEWED_SHA_MISMATCH")
        self.assertEqual(
            result["results"][0]["freshness_status"],
            "STALE_AFTER_CANDIDATE_MOVEMENT",
        )


if __name__ == "__main__":
    unittest.main()
