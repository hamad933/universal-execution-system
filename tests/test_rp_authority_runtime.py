from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ues import lifecycle_runtime as legacy
from ues.providers.base import NetworkError
from ues.rp_authority_runtime import (
    _PROVIDER_READ_UNAVAILABLE_EXIT,
    _PROVIDER_READ_UNAVAILABLE_RESULT,
    main,
    run,
)


class RPAuthorityRuntimeTests(unittest.TestCase):
    def test_wrapper_only_supplies_rp_adapter_and_grants_no_authority(self):
        original_loader = legacy._load_adapter

        def fake_observed(project: str):
            adapter = legacy._load_adapter(project)
            self.assertEqual(adapter["project"], "RP02")
            self.assertEqual(adapter["repository"], "hamad933/Enterprise-Operations-Control")
            return {
                "project": project,
                "current_authority_loaded": True,
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
            }

        with patch("ues.rp_authority_runtime._validated_authority", return_value=None), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=fake_observed
        ):
            result = run("RP02")

        self.assertIs(legacy._load_adapter, original_loader)
        self.assertEqual(result["project"], "RP02")
        self.assertEqual(result["rp_runtime_mode"], "CURRENT_AUTHORITY_GATED")
        self.assertFalse(result["runtime_wrapper_grants_authority"])

    def test_empty_validated_authority_uses_no_effect_observation_path(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH",
            "lineages": {},
            "generation_policy": {
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
        }
        expected = {
            "project": "RP01",
            "current_authority_loaded": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_live_read_performed": False,
        }
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.run_observation_backed_no_effect_health",
            return_value=expected,
        ) as health, patch("ues.rp_authority_runtime.observed.run") as live:
            result = run("RP01")

        health.assert_called_once()
        self.assertEqual(health.call_args.kwargs["authority"], authority)
        live.assert_not_called()
        self.assertFalse(result["provider_live_read_performed"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 0)

    def test_effect_capable_authority_keeps_live_runtime(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH",
            "lineages": {
                "W01": {"writer": {"provider_starting_branch": "main"}},
            },
        }
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.observed.run",
            return_value={"project": "RP01", "external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0},
        ) as live, patch("ues.rp_authority_runtime.run_observation_backed_no_effect_health") as health:
            result = run("RP01")

        live.assert_called_once_with("RP01")
        health.assert_not_called()
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 1)
        self.assertTrue(result["provider_inventory_snapshot_retry_pre_effect_only"])

    def test_sessions_list_network_outage_is_structured_pre_effect_waiting(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH-OUTAGE",
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
        }
        outage = NetworkError("provider network request failed", operation="jules.sessions.list")
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=outage
        ) as live:
            result = run("RP01")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["lifecycle_state"], "WAITING")
        self.assertEqual(result["provider_read_operation"], "jules.sessions.list")
        self.assertEqual(result["provider_read_error_category"], "NETWORK_ERROR")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(result["retry_condition"], "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED")
        self.assertEqual(result["current_authority_event_id"], "RP01-AUTH-OUTAGE")
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)
        self.assertEqual(result["provider_inventory_snapshot_attempt_limit"], 2)

    def test_transient_sessions_inventory_failure_recovers_on_second_snapshot(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH-RECOVER",
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
        }
        outage = NetworkError("provider network request failed", operation="jules.sessions.list")
        success = {"project": "RP01", "external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0}
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=[outage, success]
        ) as live:
            result = run("RP01")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)
        self.assertEqual(result["provider_inventory_snapshot_attempt_limit"], 2)
        self.assertTrue(result["provider_inventory_snapshot_retry_pre_effect_only"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_non_inventory_network_error_is_not_reclassified_or_retried(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH",
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
        }
        outage = NetworkError("provider network request failed", operation="jules.sessions.get")
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=outage
        ) as live:
            with self.assertRaises(NetworkError):
                run("RP01")
        live.assert_called_once_with("RP01")

    def test_snapshot_attempt_limit_is_bounded(self):
        with patch.dict(os.environ, {"UES_RP_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS": "99"}, clear=False):
            from ues.rp_authority_runtime import _provider_inventory_snapshot_attempts
            self.assertEqual(_provider_inventory_snapshot_attempts(), 3)

    def test_cli_materializes_initial_lineage_zero_effect_receipt_then_fails_closed(self):
        lifecycle = {
            "project": "RP01",
            "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
            "current_authority_event_id": "RP01-AUTH-OUTAGE",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                with patch("ues.rp_authority_runtime.run", return_value=lifecycle):
                    rc = main(["RP01"])
                initial = json.loads(Path("initial-lineage-result.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)

        self.assertEqual(rc, _PROVIDER_READ_UNAVAILABLE_EXIT)
        self.assertEqual(initial["result"], "INITIAL_LINEAGE_RUNTIME_BLOCKED_PROVIDER_READ_UNAVAILABLE")
        self.assertEqual(initial["external_effects_dispatched"], 0)
        self.assertEqual(initial["new_tasks_or_sessions_created"], 0)
        self.assertFalse(initial["provider_write_attempted"])
        self.assertFalse(initial["safe_to_blind_retry"])
        self.assertEqual(initial["authority_event_id"], "RP01-AUTH-OUTAGE")

    def test_non_rp_project_is_rejected(self):
        with self.assertRaises(ValueError):
            run("GS")


if __name__ == "__main__":
    unittest.main()