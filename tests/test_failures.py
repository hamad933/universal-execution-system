import unittest

from ues.failures import (
    classify_failure,
    collapse_failure_cascade,
    scope_blocker,
)


class FailureClassificationTests(unittest.TestCase):
    def test_candidate_format_failure_stays_local(self):
        result = classify_failure({"origin": "candidate", "stage": "format"})
        self.assertEqual(result["category"], "CANDIDATE_FORMAT_DEFECT")
        scope = scope_blocker(result, "W1")
        self.assertEqual(scope["blocks"], ["W1"])
        self.assertEqual(scope["remediation_owner"], "CURRENT_WORKSTREAM")

    def test_baseline_failure_routes_to_shared_lane(self):
        result = classify_failure(
            {"origin": "candidate", "stage": "test", "base_reproduces": True}
        )
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


class FailureCascadeR2Tests(unittest.TestCase):
    def test_explicit_shared_incident_collapses_to_one_blocker(self):
        failures = [
            {
                "incident_id": "shared-77",
                "origin": "infrastructure",
                "job": "php",
                "project": "GS",
                "route": "PERSONAL:GS",
                "workstream": "W01",
            },
            {
                "incident_id": "shared-77",
                "origin": "infrastructure",
                "job": "browser",
                "project": "CEP",
                "route": "PERSONAL:CEP",
                "workstream": "W03",
            },
            {
                "incident_id": "shared-77",
                "origin": "infrastructure",
                "job": "release",
                "lane_id": "lane-fcp-w02",
            },
        ]
        result = collapse_failure_cascade(failures)
        self.assertEqual(len(result["shared_blockers"]), 1)
        self.assertEqual(result["shared_blockers"][0]["incident_id"], "shared-77")
        self.assertEqual(result["shared_blockers"][0]["failure_count"], 3)
        self.assertEqual(
            result["affected_lanes"]["shared-77"],
            ["CEP|PERSONAL:CEP|W03", "GS|PERSONAL:GS|W01", "lane-fcp-w02"],
        )
        self.assertEqual(result["unshared_failures"], [])
        self.assertEqual(result["correction_task_count"], 0)
        self.assertFalse(result["duplicate_corrections"])

    def test_same_text_with_different_root_ids_does_not_collapse(self):
        failures = [
            {
                "incident_id": "root-a",
                "message": "database unavailable",
                "workstream": "W01",
            },
            {
                "incident_id": "root-b",
                "message": "database unavailable",
                "workstream": "W02",
            },
        ]
        result = collapse_failure_cascade(failures)
        self.assertEqual(result["shared_blockers"], [])
        self.assertEqual(result["affected_lanes"], {})
        self.assertEqual(result["unshared_failures"], failures)

    def test_same_text_without_structured_root_never_collapses(self):
        failures = [
            {"message": "database unavailable", "job": "php"},
            {"message": "database unavailable", "job": "browser"},
        ]
        result = collapse_failure_cascade(failures)
        self.assertEqual(result["shared_blockers"], [])
        self.assertEqual(result["unshared_failures"], failures)

    def test_mixed_candidate_and_infrastructure_roots_remain_separate(self):
        failures = [
            {
                "incident_id": "candidate-1",
                "origin": "candidate",
                "stage": "test",
                "workstream": "GS-W01",
            },
            {
                "incident_id": "infra-1",
                "origin": "infrastructure",
                "transient": False,
                "retry_count": 2,
                "workstream": "CEP-W03",
            },
        ]
        result = collapse_failure_cascade(failures)
        self.assertEqual(result["shared_blockers"], [])
        self.assertEqual(result["unshared_failures"], failures)

    def test_root_evidence_identity_groups_without_creating_work(self):
        failures = [
            {"root_evidence_id": "artifact-lineage-9", "workstream": "W01"},
            {"root_evidence_id": "artifact-lineage-9", "workstream": "W02"},
        ]
        result = collapse_failure_cascade(failures)
        self.assertEqual(
            result["shared_blockers"][0]["incident_id"],
            "artifact-lineage-9",
        )
        self.assertEqual(result["correction_task_count"], 0)
        self.assertFalse(result["duplicate_corrections"])


if __name__ == "__main__":
    unittest.main()
