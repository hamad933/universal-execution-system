from __future__ import annotations

import unittest

from ues.policy_resolution import resolve_execution_policy


class CurrentPolicyResolutionTests(unittest.TestCase):
    def adapter(self, project: str, *, ceiling: int, unknown: str) -> dict:
        return {
            "project": project,
            "route": "PERSONAL:CEP" if project == "CEP" else "GS",
            "task_budget": {
                "ceiling": ceiling,
                "reserve_target": 0,
                "unknown_lifetime_capacity": unknown,
                "automatic_new_task_creation": False,
            },
        }

    def test_cep_current_authority_outranks_stale_creation_deny_snapshot(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("CEP", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "CEP-CURRENT-OWNER-1",
                "task_budget": {"ceiling": 100, "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED"},
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "lifetime_consumption_known": False,
                "current_enumerated_tasks": 7,
                "hard_provider_limit_reached": False,
            },
        )
        value = resolved.to_dict()
        self.assertTrue(value["generation_allowed"])
        self.assertEqual(value["unknown_lifetime_policy"], "ALLOW_UNLESS_DIRECT_CEILING_REACHED")
        self.assertEqual(value["provenance"]["unknown_lifetime_policy"], "governed_authority.task_budget.unknown_lifetime_capacity")
        self.assertFalse(value["provenance"]["adapter_mutable_snapshot_is_authority"])

    def test_gs_current_ceiling_40_outranks_stale_30(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-G95",
                "task_budget": {"ceiling": 40, "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED"},
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={"lifetime_consumption_known": False, "current_enumerated_tasks": 5},
        )
        value = resolved.to_dict()
        self.assertEqual(value["ceiling"], 40)
        self.assertTrue(value["generation_allowed"])

    def test_direct_hard_ceiling_always_blocks(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-G95",
                "task_budget": {"ceiling": 40, "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED"},
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "lifetime_consumption_known": False,
                "current_enumerated_tasks": 12,
                "hard_provider_limit_reached": True,
            },
        )
        self.assertFalse(resolved.generation_allowed)
        self.assertEqual(resolved.budget["state"], "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED")

    def test_unknown_write_state_blocks_effect_even_when_policy_allows(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("CEP", ceiling=100, unknown="ALLOW_UNLESS_DIRECT_CEILING_REACHED"),
            governed_authority={
                "current": True,
                "authority_event_id": "CEP-CURRENT-OWNER-1",
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={"lifetime_consumption_known": False, "current_enumerated_tasks": 2},
            state_snapshot={"unknown_write_state": {"operation_key": "x"}},
        )
        self.assertFalse(resolved.generation_allowed)
        self.assertTrue(resolved.provenance["state_store_effect_block"])

    def test_advisory_reserve_does_not_become_hard_capacity_floor(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=40, unknown="ALLOW_UNLESS_DIRECT_CEILING_REACHED"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-G95",
                "task_budget": {
                    "ceiling": 40,
                    "reserve_target": 10,
                    "reserve_is_hard": False,
                    "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
                },
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={"lifetime_consumption_known": True, "proven_lifetime_used": 35},
        )
        self.assertTrue(resolved.generation_allowed)
        self.assertEqual(resolved.budget["safe_remaining"], 5)


if __name__ == "__main__":
    unittest.main()
