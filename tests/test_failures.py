import unittest

from ues.failures import classify_failure, scope_blocker


class FailureClassificationTests(unittest.TestCase):
    def test_candidate_format_failure_stays_local(self):
        result = classify_failure({"origin": "candidate", "stage": "format"})
        self.assertEqual(result["category"], "CANDIDATE_FORMAT_DEFECT")
        scope = scope_blocker(result, "W1")
        self.assertEqual(scope["blocks"], ["W1"])
        self.assertEqual(scope["remediation_owner"], "CURRENT_WORKSTREAM")

    def test_baseline_failure_routes_to_shared_lane(self):
        result = classify_failure({"origin": "candidate", "stage": "test", "base_reproduces": True})
        self.assertEqual(result["category"], "SHARED_BASELINE_DEFECT")
        scope = scope_blocker(result, "W1")
        self.assertEqual(scope["blocks"], [])
        self.assertEqual(scope["remediation_owner"], "SEPARATE_SHARED_LANE")

    def test_transient_infrastructure_does_not_authorize_code_write(self):
        result = classify_failure({"origin": "infrastructure", "transient": True})
        self.assertEqual(result["category"], "INFRASTRUCTURE_TRANSIENT")
        scope = scope_blocker(result, "W1")
        self.assertFalse(scope["automatic_write_authorized"])

    def test_ambiguous_failure_requires_triage(self):
        result = classify_failure({"stage": "test"})
        self.assertEqual(result["category"], "UNKNOWN_REQUIRES_TRIAGE")
        self.assertEqual(result["confidence"], "LOW")


if __name__ == "__main__":
    unittest.main()
