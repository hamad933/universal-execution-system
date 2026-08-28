from __future__ import annotations

import unittest

from ues.handoff_adjudication import exact_invalid_review_handoff_adjudication
from ues.lifecycle_runtime_v2 import _structured_handoff_recovery_ready


class ParentHandoffAdjudicationTests(unittest.TestCase):
    EVENT = "RP04-U39"
    SESSION = "a" * 64
    MESSAGE = "b" * 64
    ACTIVITY = "c" * 64

    def _handoff(self):
        return {
            "role": "REVIEWER",
            "workstream": "RP04-IPA-S03-001",
            "verdict": "UNKNOWN",
            "message_fingerprint": self.MESSAGE,
            "activity_fingerprint": self.ACTIVITY,
        }

    def _state(self, generation: int = 3):
        return {
            "generation": generation,
            "session_fingerprint": self.SESSION,
            "unknown_write_state": False,
            "action_in_flight": False,
        }

    def _binding(self):
        return {
            "status": "PROVEN",
            "provider_state": "COMPLETED",
            "session_fingerprint": self.SESSION,
        }

    def _lane(self, **overrides):
        adjudication = {
            "classification": "INVALID_FOR_REVIEW_EVIDENCE",
            "authority_event_id": self.EVENT,
            "project": "RP04",
            "route": "RP04",
            "workstream": "RP04-IPA-S03-001",
            "role": "REVIEWER",
            "generation": 3,
            "session_fingerprint": self.SESSION,
            "handoff_message_fingerprint": self.MESSAGE,
            "handoff_activity_fingerprint": self.ACTIVITY,
        }
        adjudication.update(overrides)
        return {"authorized": True, "handoff_adjudication": adjudication}

    def _adjudicated(self, lane=None, state=None, binding=None):
        return exact_invalid_review_handoff_adjudication(
            authority_event_id=self.EVENT,
            lane_authority=lane or self._lane(),
            project="RP04",
            route="RP04",
            workstream="RP04-IPA-S03-001",
            role="REVIEWER",
            handoff=self._handoff(),
            binding=binding or self._binding(),
            state_snapshot=state or self._state(),
        )

    def test_exact_parent_adjudication_allows_same_lineage_recovery_readiness(self):
        invalidated = self._adjudicated()
        self.assertTrue(invalidated)
        self.assertTrue(
            _structured_handoff_recovery_ready(
                role="REVIEWER",
                binding=self._binding(),
                handoff=self._handoff(),
                state_snapshot=self._state(),
                handoff_invalidated=invalidated,
            )
        )

    def test_valid_handoff_without_explicit_adjudication_still_blocks(self):
        self.assertFalse(
            _structured_handoff_recovery_ready(
                role="REVIEWER",
                binding=self._binding(),
                handoff=self._handoff(),
                state_snapshot=self._state(),
            )
        )

    def test_stale_generation_or_session_adjudication_fails_closed(self):
        self.assertFalse(self._adjudicated(state=self._state(generation=4)))
        stale_binding = self._binding()
        stale_binding["session_fingerprint"] = "d" * 64
        self.assertFalse(self._adjudicated(binding=stale_binding))

    def test_wrong_authority_event_or_handoff_fingerprint_fails_closed(self):
        self.assertFalse(self._adjudicated(lane=self._lane(authority_event_id="RP04-U40")))
        self.assertFalse(self._adjudicated(lane=self._lane(handoff_message_fingerprint="d" * 64)))


if __name__ == "__main__":
    unittest.main()
