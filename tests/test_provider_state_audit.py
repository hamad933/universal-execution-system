from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.identity import canonical_lane_id
from ues.provider_state_audit import audit_durable_provider_state
from ues.provider_targets import ProjectTarget
from ues.state_store import StateRead, WorkstreamRuntimeRecord


class FakeStore:
    def __init__(self):
        self.records = {}
        for project, route, repo, state, branch, digest in (
            (
                "CEP",
                "PERSONAL:CEP",
                "hamad933/Cybersecurity-Education-Platform",
                "AWAITING_USER_FEEDBACK",
                "work/cep-w04-parent-reconciliation-r02",
                "a" * 64,
            ),
            ("GS", "GS", "hamad933/GS-2", "FAILED", "review/gs-home", "b" * 64),
        ):
            workstream = f"PROVIDER-SESSION-{digest.upper()}"
            lane_id = canonical_lane_id(project, route, workstream)
            record = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project=project,
                route=route,
                workstream_id=workstream,
                activation_mode="SHADOW",
                last_observed_provider_state={
                    "provider": "JULES",
                    "state": state,
                    "classification": (
                        "CONTROLLER_INPUT_RECONCILIATION_REQUIRED"
                        if state == "AWAITING_USER_FEEDBACK"
                        else "TERMINAL_FAILURE_RECONCILIATION_REQUIRED"
                    ),
                    "session_identity_hash": digest,
                    "repository": repo,
                    "starting_branch": branch,
                    "new_waiting_activity_after_prior_user_response": state == "AWAITING_USER_FEEDBACK",
                },
            )
            self.records[lane_id] = record

    def discover_lane_ids(self):
        return tuple(self.records)

    def read_workstream(self, lane_id):
        return StateRead("OK", 1, self.records[lane_id], "SHADOW", False, None, False)


class ProviderStateAuditTests(unittest.TestCase):
    def test_audit_surfaces_waiting_and_terminal_provider_lanes_without_mutation(self):
        targets = (
            ProjectTarget("GS", "GS", "hamad933/GS-2", "GS_SHADOW_V2"),
            ProjectTarget(
                "CEP",
                "PERSONAL:CEP",
                "hamad933/Cybersecurity-Education-Platform",
                "CEP_SHADOW_V2",
            ),
        )
        with patch("ues.provider_state_audit.load_project_targets", return_value=targets):
            result = audit_durable_provider_state(store=FakeStore())
        self.assertEqual(result["provider_lane_count"], 2)
        self.assertEqual(result["blocked_provider_lane_count"], 2)
        self.assertEqual(result["waiting_provider_lane_count"], 1)
        self.assertEqual(result["new_waiting_activity_after_prior_user_response_count"], 1)
        self.assertEqual(result["project_state_counts"]["CEP"], {"AWAITING_USER_FEEDBACK": 1})
        self.assertEqual(result["project_state_counts"]["GS"], {"FAILED": 1})
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["raw_session_identity_emitted"])


if __name__ == "__main__":
    unittest.main()
