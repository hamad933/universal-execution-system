from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from ues.terminal_results import extract_terminal_candidate, materialize_project_results


SHA_A = "a" * 40
SHA_B = "b" * 40
ATTENTION = "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK"


@dataclass
class _Record:
    project: str
    route: str
    evidence_bindings: dict
    operation_receipt: dict | None = None


@dataclass
class _Read:
    status: str
    record: _Record | None


class _Store:
    def __init__(self, records):
        self.records = records

    def discover_lane_ids(self):
        return list(self.records)

    def read_workstream(self, lane_id):
        record = self.records.get(lane_id)
        return _Read("OK", record) if record else _Read("MISSING", None)


def _handoff(*, workstream="S01", role="REVIEWER", reviewed_sha=SHA_A, findings=None):
    payload = {
        "role": role,
        "workstream": workstream,
        "status": "COMPLETE",
        "verdict": "FINDINGS" if findings else "PASS",
        "candidate_sha": None,
        "reviewed_sha": reviewed_sha,
        "context_state": "OK",
        "findings": findings or [],
    }
    message = "done\n<UES_HANDOFF_V1>\n" + json.dumps(payload) + "\n</UES_HANDOFF_V1>"
    return [{"name": "activities/1", "agentMessaged": {"agentMessage": message}}]


def _store(*, project="RP04", route="RP04", fp="fp-1", workstream="S01", role="REVIEWER", sha=SHA_A):
    return _Store({"lane-1": _Record(project, route, {
        "session_fingerprint": fp,
        "role": role,
        "workstream": workstream,
        "generation": 1,
        "current_candidate_sha": sha,
        "current_pr_number": 10,
    })})


def _snapshot(candidate, *, project="RP04", route="RP04", repository="owner/repo", fp="fp-1"):
    return {
        "project": project,
        "route": route,
        "repository": repository,
        "provider_read_complete": True,
        "provider_mutation_performed": False,
        "sessions": [{
            "session_fingerprint": fp,
            "state": "COMPLETED",
            "classification": ATTENTION,
            "source_repository": repository,
            "source_binding_proven": True,
            "_terminal_candidate": candidate,
        }],
    }


class TerminalResultMaterializationTests(unittest.TestCase):
    def test_completed_reviewer_valid_handoff_becomes_parent_consumable(self):
        findings = [
            {"id": "F-1", "severity": "high", "path": "app/a.py", "locator": "L10", "detail": "first actionable finding", "recommended_remediation": "fix A", "evidence_references": ["artifact:1"]},
            {"id": "F-2", "severity": "medium", "resource": "page:S01", "detail": "second actionable finding", "recommended_action": "fix B"},
        ]
        candidate = extract_terminal_candidate(_handoff(findings=findings))
        result = materialize_project_results(_snapshot(candidate), _store())
        self.assertEqual(result["parent_consumable_result_count"], 1)
        bound = result["results"][0]
        self.assertEqual(bound["result_state"], "PARENT_CONSUMABLE")
        self.assertEqual(bound["reviewed_sha"], SHA_A)
        self.assertEqual(bound["finding_count"], 2)
        self.assertEqual([item["finding_id"] for item in bound["findings"]], ["F-1", "F-2"])
        self.assertEqual(bound["findings"][0]["recommended_action"], "fix A")
        self.assertEqual(result["sessions"][0]["classification"], "COMPLETED_OUTPUT_CONSUMED")
        self.assertFalse(result["attention_required"])

    def test_exact_lineage_workstream_mismatch_rejected(self):
        candidate = extract_terminal_candidate(_handoff(workstream="WRONG"))
        result = materialize_project_results(_snapshot(candidate), _store())
        self.assertEqual(result["results"][0]["result_state"], "STRUCTURED_HANDOFF_UNBOUND")
        self.assertEqual(result["sessions"][0]["classification"], ATTENTION)
        self.assertTrue(result["attention_required"])
        self.assertEqual(result["parent_consumable_result_count"], 0)

    def test_wrong_reviewed_sha_is_stale_not_consumable(self):
        candidate = extract_terminal_candidate(_handoff(reviewed_sha=SHA_B))
        result = materialize_project_results(_snapshot(candidate), _store())
        bound = result["results"][0]
        self.assertEqual(bound["result_state"], "REVIEWED_SHA_MISMATCH")
        self.assertEqual(bound["freshness_status"], "STALE_AFTER_CANDIDATE_MOVEMENT")
        self.assertEqual(result["sessions"][0]["classification"], ATTENTION)

    def test_malformed_handoff_fails_closed(self):
        candidate = extract_terminal_candidate([{"agentMessaged": {"agentMessage": "<UES_HANDOFF_V1>{bad json}</UES_HANDOFF_V1>"}}])
        self.assertEqual(candidate["state"], "MALFORMED_STRUCTURED_HANDOFF")
        result = materialize_project_results(_snapshot(candidate), _store())
        self.assertEqual(result["results"][0]["result_state"], "MALFORMED_STRUCTURED_HANDOFF")
        self.assertIsNone(result["results"][0]["verdict"])
        self.assertTrue(result["attention_required"])

    def test_legacy_completed_without_marker_is_explicitly_unstructured(self):
        candidate = extract_terminal_candidate([{"agentMessaged": {"agentMessage": "legacy reviewer prose only"}}])
        self.assertEqual(candidate["state"], "COMPLETED_OUTPUT_UNSTRUCTURED")
        result = materialize_project_results(_snapshot(candidate), _store())
        self.assertEqual(result["results"][0]["result_state"], "COMPLETED_OUTPUT_UNSTRUCTURED")
        self.assertTrue(result["results"][0]["safe_read_only_recovery_exists"])
        self.assertTrue(result["attention_required"])

    def test_unproven_session_fingerprint_is_not_bound_by_elimination(self):
        candidate = extract_terminal_candidate(_handoff())
        result = materialize_project_results(_snapshot(candidate, fp="unknown"), _store())
        session = result["sessions"][0]
        self.assertEqual(session["result_state"], "RESULT_IDENTITY_UNRESOLVED")
        self.assertEqual(session["classification"], ATTENTION)
        self.assertEqual(result["results"][0]["identity_reason"], "NO_EXACT_LINEAGE_MATCH")
        self.assertEqual(result["parent_consumable_result_count"], 0)

    def test_duplicate_observation_is_idempotent(self):
        candidate = extract_terminal_candidate(_handoff())
        first = materialize_project_results(_snapshot(candidate), _store())
        second = materialize_project_results(_snapshot(candidate), _store())
        self.assertEqual(first["results"][0]["result_fingerprint"], second["results"][0]["result_fingerprint"])

    def test_sanitizer_does_not_persist_secret_bearing_finding_values(self):
        candidate = extract_terminal_candidate(_handoff(findings=[{
            "id": "F-secret",
            "severity": "high",
            "detail": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "recommended_action": "use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        }]))
        rendered = json.dumps(candidate)
        self.assertNotIn("Bearer abcdef", rendered)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_project_route_scope_prevents_cross_project_identity_leakage(self):
        candidate = extract_terminal_candidate(_handoff())
        foreign = _store(project="RP03", route="RP03")
        result = materialize_project_results(_snapshot(candidate), foreign)
        self.assertEqual(result["sessions"][0]["result_state"], "RESULT_IDENTITY_UNRESOLVED")
        self.assertEqual(result["results"][0]["identity_reason"], "NO_EXACT_LINEAGE_MATCH")
        self.assertEqual(result["parent_consumable_result_count"], 0)

    def test_source_repository_binding_must_be_proven(self):
        candidate = extract_terminal_candidate(_handoff())
        snapshot = _snapshot(candidate)
        snapshot["sessions"][0]["source_binding_proven"] = False
        result = materialize_project_results(snapshot, _store())
        self.assertEqual(result["results"][0]["identity_reason"], "SOURCE_REPOSITORY_BINDING_UNPROVEN")
        self.assertEqual(result["parent_consumable_result_count"], 0)

    def test_completed_activity_read_outage_remains_recoverable_zero_effect_state(self):
        candidate = {"structured": False, "state": "COMPLETED_OUTPUT_UNCONSUMED"}
        result = materialize_project_results(_snapshot(candidate), _store())
        bound = result["results"][0]
        self.assertEqual(bound["result_state"], "COMPLETED_OUTPUT_UNCONSUMED")
        self.assertTrue(bound["safe_read_only_recovery_exists"])
        self.assertEqual(result["provider_mutation_performed"], False)
        self.assertTrue(result["attention_required"])


if __name__ == "__main__":
    unittest.main()
