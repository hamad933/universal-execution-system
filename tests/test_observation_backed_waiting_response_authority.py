from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.observation_backed_health import observation_backed_no_effect_eligible


class ObservationBackedWaitingResponseAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = {
            "project": "RP02",
            "route": "RP02",
            "repository": "hamad933/enterprise-operations",
        }
        self.zero_effect_authority = {
            "lineages": {
                "RP02-S04-CORRECTION": {
                    "writer": {"provider_starting_branch": "feature/rp02"}
                }
            },
            "generation_policy": {
                "necessary_generation_authorized": False,
                "generation_effect_authorized": False,
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
            "workflow_dispatches": {},
        }

    def eligible(self, authority: dict[str, object]) -> bool:
        with patch("ues.observation_backed_health.legacy._lineage_runtime", return_value={}):
            return observation_backed_no_effect_eligible(self.adapter, authority)

    def test_controller_resolvable_waiting_response_requires_live_lifecycle(self):
        authority = {
            **self.zero_effect_authority,
            "waiting_responses": {
                "RP02-S04-CORRECTION:WRITER": {
                    "controller_resolvable": True,
                    "scope_expansion": False,
                    "response": "Continue the same bounded Writer lineage.",
                }
            },
        }
        self.assertFalse(self.eligible(authority))

    def test_no_waiting_response_keeps_explicit_zero_effect_authority_eligible(self):
        self.assertTrue(self.eligible(dict(self.zero_effect_authority)))

    def test_non_effect_waiting_entries_do_not_manufacture_provider_authority(self):
        for entry in (
            {"controller_resolvable": False, "scope_expansion": False, "response": "continue"},
            {"controller_resolvable": True, "scope_expansion": True, "response": "continue"},
            {"controller_resolvable": True, "scope_expansion": False, "response": "   "},
            "not-an-object",
        ):
            with self.subTest(entry=entry):
                authority = {
                    **self.zero_effect_authority,
                    "waiting_responses": {"RP02-S04-CORRECTION:WRITER": entry},
                }
                self.assertTrue(self.eligible(authority))


if __name__ == "__main__":
    unittest.main()
