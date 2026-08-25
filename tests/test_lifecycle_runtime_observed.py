from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ues import lifecycle_runtime as legacy
from ues.lifecycle_runtime_observed import (
    _PROVIDER_READ_UNAVAILABLE_EXIT,
    _PROVIDER_READ_UNAVAILABLE_RESULT,
    _persist_health_with_runtime_binding,
    main,
    run,
    runtime_binding_from_env,
)
from ues.providers.base import NetworkError, RateLimitError, ServerError
from ues.state_store import DeterministicFileStateStore


class LifecycleRuntimeObservedTests(unittest.TestCase):
    def test_github_actions_runtime_binding_is_exact_and_sanitized(self) -> None:
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "hamad933/universal-execution-system",
            "GITHUB_SHA": "A" * 40,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_WORKFLOW_REF": "hamad933/universal-execution-system/.github/workflows/ues-bounded-existing-session.yml@refs/heads/main",
            "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_TOKEN": "must-not-persist",
        }
        binding = runtime_binding_from_env(env)
        self.assertEqual(binding["status"], "BOUND")
        self.assertEqual(binding["repository"], "hamad933/universal-execution-system")
        self.assertEqual(binding["sha"], "a" * 40)
        self.assertEqual(binding["ref"], "refs/heads/main")
        self.assertEqual(binding["ref_name"], "main")
        self.assertEqual(binding["run_id"], 123456)
        self.assertEqual(binding["run_attempt"], 2)
        self.assertTrue(binding["telemetry_grants_no_authority"])
        self.assertNotIn("GITHUB_TOKEN", binding)
        self.assertNotIn("must-not-persist", repr(binding))

    def test_invalid_or_non_actions_environment_fails_closed_to_unbound(self) -> None:
        for env in (
            {},
            {"GITHUB_ACTIONS": "false", "GITHUB_REPOSITORY": "hamad933/universal-execution-system", "GITHUB_SHA": "a" * 40},
            {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "bad repository", "GITHUB_SHA": "a" * 40},
            {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "hamad933/universal-execution-system", "GITHUB_SHA": "not-a-sha"},
        ):
            with self.subTest(env=env):
                binding = runtime_binding_from_env(env)
                self.assertEqual(binding["status"], "UNBOUND")
                self.assertTrue(binding["telemetry_grants_no_authority"])
                self.assertNotIn("sha", binding)

    def test_health_receipt_persists_runtime_binding_without_granting_authority(self) -> None:
        binding = {
            "status": "BOUND",
            "repository": "hamad933/universal-execution-system",
            "sha": "b" * 40,
            "ref": "refs/heads/main",
            "source": "GITHUB_ACTIONS_RUNTIME_ENV",
            "telemetry_grants_no_authority": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            persist = _persist_health_with_runtime_binding(legacy._persist_health, binding)
            result = persist(
                store,
                project="CEP",
                route="PERSONAL:CEP",
                status="PASS",
                summary={"external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0},
            )
            lane_id = legacy.canonical_lane_id("CEP", "PERSONAL:CEP", legacy.HEALTH_WORKSTREAM)
            read = store.read_workstream(lane_id)

        self.assertEqual(result["runtime_binding_status"], "BOUND")
        self.assertEqual(result["runtime_sha"], "b" * 40)
        self.assertEqual(read.status, "OK")
        self.assertIsNotNone(read.record)
        record = read.record
        assert record is not None
        self.assertEqual(record.activation_mode, "SHADOW")
        self.assertEqual(record.last_observed_github_state["sha"], "b" * 40)
        self.assertTrue(record.last_observed_github_state["telemetry_grants_no_authority"])
        self.assertTrue(record.authority_provenance["runtime_binding_grants_no_authority"])
        self.assertEqual(record.last_observed_provider_state["summary"]["external_effects_dispatched"], 0)
        self.assertEqual(record.last_observed_provider_state["summary"]["new_tasks_or_sessions_created"], 0)

    def test_unbound_runtime_marker_replaces_stale_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            bound = _persist_health_with_runtime_binding(
                legacy._persist_health,
                {
                    "status": "BOUND",
                    "repository": "hamad933/universal-execution-system",
                    "sha": "c" * 40,
                    "source": "GITHUB_ACTIONS_RUNTIME_ENV",
                    "telemetry_grants_no_authority": True,
                },
            )
            bound(store, project="GS", route="GS", status="PASS", summary={"phase": "OLD"})
            unbound = _persist_health_with_runtime_binding(
                legacy._persist_health,
                {
                    "status": "UNBOUND",
                    "source": "GITHUB_ACTIONS_RUNTIME_ENV",
                    "telemetry_grants_no_authority": True,
                },
            )
            unbound(store, project="GS", route="GS", status="PASS", summary={"phase": "NEW"})
            lane_id = legacy.canonical_lane_id("GS", "GS", legacy.HEALTH_WORKSTREAM)
            read = store.read_workstream(lane_id)

        self.assertEqual(read.status, "OK")
        self.assertIsNotNone(read.record)
        record = read.record
        assert record is not None
        self.assertEqual(record.last_observed_github_state["status"], "UNBOUND")
        self.assertNotIn("sha", record.last_observed_github_state)
        self.assertEqual(record.last_observed_provider_state["summary"]["phase"], "NEW")

    def test_pre_effect_inventory_outages_become_structured_zero_effect_results(self) -> None:
        for error_type in (NetworkError, RateLimitError, ServerError):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("provider read unavailable", operation="jules.sessions.list")
                with patch("ues.lifecycle_runtime_observed.current.run", side_effect=error):
                    result = run("CEP")

                self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
                self.assertEqual(result["lifecycle_state"], "WAITING")
                self.assertFalse(result["provider_read_authoritative"])
                self.assertEqual(result["provider_read_operation"], "jules.sessions.list")
                self.assertEqual(result["provider_read_error_category"], error.category)
                self.assertFalse(result["provider_write_attempted"])
                self.assertEqual(result["external_effects_dispatched"], 0)
                self.assertEqual(result["new_tasks_or_sessions_created"], 0)
                self.assertEqual(result["retry_condition"], "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED")
                self.assertFalse(result["safe_to_blind_retry"])
                self.assertFalse(result["raw_session_ids_persisted"])
                self.assertFalse(result["runtime_binding_grants_authority"])
                self.assertEqual(result["observability_schema_version"], "1.0")

    def test_non_inventory_provider_failure_is_not_reclassified(self) -> None:
        error = NetworkError("write readback unavailable", operation="jules.sessions.sendMessage")
        with patch("ues.lifecycle_runtime_observed.current.run", side_effect=error):
            with self.assertRaises(NetworkError) as raised:
                run("GS")
        self.assertIs(raised.exception, error)

    def test_cli_keeps_provider_outage_fail_closed_with_structured_output(self) -> None:
        result = {
            "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }
        output = io.StringIO()
        with patch("ues.lifecycle_runtime_observed.run", return_value=result), redirect_stdout(output):
            exit_code = main(["CEP"])

        self.assertEqual(exit_code, _PROVIDER_READ_UNAVAILABLE_EXIT)
        self.assertEqual(json.loads(output.getvalue())["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)


if __name__ == "__main__":
    unittest.main()
