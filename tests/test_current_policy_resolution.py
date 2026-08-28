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
                "unknown_quota_window_capacity": unknown,
                "automatic_new_task_creation": False,
            },
        }

    def test_cep_current_authority_outranks_stale_creation_deny_snapshot(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("CEP", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "CEP-CURRENT-OWNER-1",
                "task_budget": {
                    "ceiling": 100,
                    "unknown_quota_window_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
                },
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": False,
                "current_window_enumerated_tasks": 7,
                "provider_inventory_total": 60,
                "hard_provider_limit_reached": False,
            },
        )
        value = resolved.to_dict()
        self.assertTrue(value["generation_allowed"])
        self.assertEqual(value["unknown_quota_window_policy"], "ALLOW_UNLESS_DIRECT_CEILING_REACHED")
        self.assertEqual(
            value["provenance"]["unknown_quota_window_policy"],
            "governed_authority.task_budget.unknown_quota_window_capacity",
        )
        self.assertEqual(value["budget"]["observed_used_lower_bound"], 7)
        self.assertFalse(value["budget"]["historical_usage_affects_capacity"])
        self.assertFalse(value["provenance"]["adapter_mutable_snapshot_is_authority"])

    def test_current_ceiling_outranks_stale_adapter_value(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "task_budget": {
                    "ceiling": 40,
                    "unknown_quota_window_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
                },
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": False,
                "current_window_enumerated_tasks": 5,
            },
        )
        value = resolved.to_dict()
        self.assertEqual(value["ceiling"], 40)
        self.assertTrue(value["generation_allowed"])

    def test_direct_hard_ceiling_always_blocks(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=30, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "task_budget": {
                    "ceiling": 40,
                    "unknown_quota_window_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
                },
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": False,
                "current_window_enumerated_tasks": 12,
                "hard_provider_limit_reached": True,
            },
        )
        self.assertFalse(resolved.generation_allowed)
        self.assertEqual(resolved.budget["state"], "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED")

    def test_runtime_resolved_ceiling_missing_is_distinct_fail_closed_state(self) -> None:
        adapter = {
            "project": "RP03",
            "route": "RP03",
            "task_budget": {
                "current_ceiling_must_be_resolved_at_runtime": True,
                "runtime_budget_preflight_required": True,
                "unknown_lifetime_capacity": "DENY",
            },
        }
        resolved = resolve_execution_policy(
            adapter=adapter,
            governed_authority={
                "current": True,
                "authority_event_id": "RP03-CURRENT",
                "generation_policy": {
                    "necessary_generation_authorized": True,
                    "generation_effect_authorized": True,
                },
            },
            provider_observation={
                "quota_window_consumption_known": True,
                "proven_quota_window_used": 8,
                "current_window_enumerated_tasks": 8,
                "hard_provider_limit_reached": False,
            },
        )
        value = resolved.to_dict()
        self.assertIsNone(value["ceiling"])
        self.assertFalse(value["provenance"]["ceiling_resolved"])
        self.assertEqual(value["provenance"]["ceiling"], "unresolved")
        self.assertEqual(value["budget"]["state"], "CAPACITY_CEILING_UNRESOLVED")
        self.assertFalse(value["budget"]["hard_ceiling_reached"])
        self.assertFalse(value["generation_budget_safe"])
        self.assertFalse(value["generation_allowed"])

    def test_explicit_zero_ceiling_remains_an_actual_boundary(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=0, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": True,
                "proven_quota_window_used": 0,
                "current_window_enumerated_tasks": 0,
            },
        )
        self.assertEqual(resolved.ceiling, 0)
        self.assertTrue(resolved.provenance["ceiling_resolved"])
        self.assertTrue(resolved.budget["hard_ceiling_reached"])
        self.assertEqual(resolved.budget["state"], "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED")

    def test_unknown_write_state_blocks_effect_even_when_policy_allows(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("CEP", ceiling=100, unknown="ALLOW_UNLESS_DIRECT_CEILING_REACHED"),
            governed_authority={
                "current": True,
                "authority_event_id": "CEP-CURRENT-OWNER-1",
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": False,
                "current_window_enumerated_tasks": 2,
            },
            state_snapshot={"unknown_write_state": {"operation_key": "x"}},
        )
        self.assertFalse(resolved.generation_allowed)
        self.assertTrue(resolved.provenance["state_store_effect_block"])

    def test_advisory_reserve_does_not_become_hard_capacity_floor(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=40, unknown="ALLOW_UNLESS_DIRECT_CEILING_REACHED"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "task_budget": {
                    "ceiling": 40,
                    "reserve_target": 10,
                    "reserve_is_hard": False,
                    "unknown_quota_window_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
                },
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": True,
                "proven_quota_window_used": 35,
                "current_window_enumerated_tasks": 35,
            },
        )
        self.assertTrue(resolved.generation_allowed)
        self.assertEqual(resolved.budget["safe_remaining"], 5)

    def test_prior_history_never_reduces_current_window_headroom(self) -> None:
        resolved = resolve_execution_policy(
            adapter=self.adapter("GS", ceiling=100, unknown="DENY"),
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "task_budget": {"ceiling": 100},
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": True,
                "proven_quota_window_used": 10,
                "current_window_enumerated_tasks": 10,
                "historical_outside_window_tasks": 90,
                "provider_inventory_total": 100,
            },
        )
        self.assertTrue(resolved.generation_allowed)
        self.assertEqual(resolved.budget["safe_remaining"], 90)
        self.assertEqual(resolved.budget["observed_used_lower_bound"], 10)
        self.assertFalse(resolved.budget["historical_usage_affects_capacity"])

    def test_legacy_unknown_policy_key_remains_compatible(self) -> None:
        adapter = {
            "project": "GS",
            "route": "GS",
            "task_budget": {
                "ceiling": 40,
                "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
            },
        }
        resolved = resolve_execution_policy(
            adapter=adapter,
            governed_authority={
                "current": True,
                "authority_event_id": "GS-CURRENT",
                "generation_policy": {"necessary_generation_authorized": True},
            },
            provider_observation={
                "quota_window_consumption_known": False,
                "current_window_enumerated_tasks": 1,
            },
        )
        value = resolved.to_dict()
        self.assertTrue(value["generation_allowed"])
        self.assertEqual(value["unknown_lifetime_policy"], value["unknown_quota_window_policy"])


if __name__ == "__main__":
    unittest.main()
