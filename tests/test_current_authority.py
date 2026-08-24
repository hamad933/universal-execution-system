from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ues.current_authority import CurrentAuthorityError, exact_lineage_authority, validate_current_authority


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
                }
            },
        }

    def test_exact_allowlisted_controller_transport_is_accepted(self):
        value = validate_current_authority(
            self.adapter(),
            self.envelope(),
            transport_actor="hamad933",
            now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(value["transport"]["canonical_truth_owner"], "DRIVE")
        lane = exact_lineage_authority(value, workstream="W02", role="WRITER")
        self.assertIsNotNone(lane)

    def test_event_transport_does_not_authorize_unlisted_lineage(self):
        value = validate_current_authority(
            self.adapter(),
            self.envelope(),
            transport_actor="hamad933",
            now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(exact_lineage_authority(value, workstream="W03", role="WRITER"))

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
