from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ues.current_authority import (
    CurrentAuthorityError,
    exact_lineage_authority,
    initial_lineage_authority,
    validate_current_authority,
)


class CurrentAuthorityTests(unittest.TestCase):
    def adapter(self):
        return {
            "project": "CEP",
            "route": "PERSONAL:CEP",
            "authority_transport": {"controller_actor_allowlist": ["hamad933"]},
        }

    def envelope(self):
        return {
            "source": "DRIVE_CURRENT_STATE",
            "source_id": "drive:cep-current",
            "project": "CEP",
            "route": "PERSONAL:CEP",
            "current": True,
            "authority_event_id": "CEP-GATE-1",
            "expires_at": "2026-08-24T10:00:00Z",
            "generation_policy": {
                "authorized_lineages": {
                    "W02:WRITER": {
                        "authorized": True,
                        "replacement_cause": "IRRECOVERABLY_INVALID_BINDING",
                    }
                },
                "authorized_initial_lineages": {
                    "W06:WRITER": {
                        "authorized": True,
                        "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                        "task_spec": {
                            "objective": "Implement only the current governed W06 scope",
                            "write_scope": ["src/w06/**"],
                            "stop_gate": "DRAFT_PR_AND_EXACT_HEAD_CI",
                        },
                    }
                },
            },
        }

    def validate(self):
        return validate_current_authority(
            self.adapter(),
            self.envelope(),
            transport_actor="hamad933",
            now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        )

    def test_exact_allowlisted_controller_transport_is_accepted(self):
        value = self.validate()
        self.assertEqual(value["transport"]["canonical_truth_owner"], "DRIVE")
        lane = exact_lineage_authority(value, workstream="W02", role="WRITER")
        self.assertIsNotNone(lane)

    def test_event_transport_does_not_authorize_unlisted_lineage(self):
        value = self.validate()
        self.assertIsNone(exact_lineage_authority(value, workstream="W03", role="WRITER"))

    def test_explicit_initial_lineage_authority_is_separate_and_structured(self):
        value = self.validate()
        initial = initial_lineage_authority(value, workstream="W06", role="WRITER")
        self.assertIsNotNone(initial)
        self.assertEqual(initial["creation_kind"], "INITIAL_LOGICAL_LINEAGE")
        self.assertTrue(initial["task_spec"])
        self.assertIsNone(initial_lineage_authority(value, workstream="W02", role="WRITER"))

    def test_initial_lineage_authority_rejects_missing_semantics_or_task_spec(self):
        envelope = self.envelope()
        lane = envelope["generation_policy"]["authorized_initial_lineages"]["W06:WRITER"]
        lane.pop("task_spec")
        value = validate_current_authority(
            self.adapter(), envelope, transport_actor="hamad933",
            now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(initial_lineage_authority(value, workstream="W06", role="WRITER"))

        envelope = self.envelope()
        envelope["generation_policy"]["authorized_initial_lineages"]["W06:WRITER"]["creation_kind"] = "REPLACEMENT"
        value = validate_current_authority(
            self.adapter(), envelope, transport_actor="hamad933",
            now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(initial_lineage_authority(value, workstream="W06", role="WRITER"))

    def test_stale_or_wrong_actor_fails_closed(self):
        with self.assertRaises(CurrentAuthorityError):
            validate_current_authority(
                self.adapter(),
                self.envelope(),
                transport_actor="someone-else",
                now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
            )
        with self.assertRaises(CurrentAuthorityError):
            validate_current_authority(
                self.adapter(),
                self.envelope(),
                transport_actor="hamad933",
                now=datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
