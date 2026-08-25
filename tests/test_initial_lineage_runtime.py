from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.initial_lineage_runtime import (
    _PROVIDER_READ_UNAVAILABLE_EXIT,
    _PROVIDER_READ_UNAVAILABLE_RESULT,
    _dynamic_provider_starting_branch,
    _dynamic_role_config,
    _marker_matches,
    _parse_exact_baseline,
    _parse_lane_key,
    _task_prompt,
    _validate_task_spec,
    main,
    run,
)
from ues.providers.base import NetworkError, RateLimitError


class InitialLineageRuntimeTests(unittest.TestCase):
    def writer_task(self):
        return {
            "objective": "Do governed work",
            "exact_baseline": "main@" + "a" * 40,
            "write_scope": ["src/**"],
            "prohibited_scope": ["deploy/**"],
            "validation": ["python -m unittest"],
            "evidence": ["exact-head-ci"],
            "handoff": "Return exact SHA and validation evidence",
            "stop_gate": "DRAFT_PR_AND_EXACT_HEAD_CI",
        }

    def initial_authority(self):
        return {
            "source": "DRIVE_CURRENT_STATE",
            "source_id": "authority-source",
            "project": "RP02",
            "route": "RP02",
            "current": True,
            "authority_event_id": "RP02-AUTH-OUTAGE",
            "generation_policy": {
                "authorized_initial_lineages": {
                    "RP02-IPA-S03:ASSURANCE": {"authorized": True, "task_spec": {}},
                }
            },
        }

    def test_exact_baseline_is_branch_and_full_sha_only(self):
        ref, sha = _parse_exact_baseline({"exact_baseline": "main@" + "A" * 40})
        self.assertEqual(ref, "main")
        self.assertEqual(sha, "a" * 40)
        ref, _ = _parse_exact_baseline({"exact_baseline": "refs/heads/work/w11@" + "b" * 40})
        self.assertEqual(ref, "work/w11")
        for invalid in (
            "main",
            "main@abc",
            "../main@" + "a" * 40,
            "refs/tags/v1@" + "a" * 40,
            "refs/pull/1/head@" + "a" * 40,
            "main@{" + "a" * 40,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _parse_exact_baseline({"exact_baseline": invalid})

    def test_initial_authority_key_and_dynamic_topology_are_both_required(self):
        self.assertEqual(_parse_lane_key("IPA-S01:REVIEWER"), ("IPA-S01", "REVIEWER"))
        with self.assertRaises(ValueError):
            _parse_lane_key("W11:UNKNOWN")
        authority = {
            "lineages": {
                "W11": {
                    "writer": {"provider_starting_branch": "main"},
                }
            }
        }
        role = _dynamic_role_config(authority, workstream="W11", role="WRITER")
        self.assertIsNotNone(role)
        assert role is not None
        self.assertEqual(_dynamic_provider_starting_branch(role), "main")
        self.assertIsNone(_dynamic_role_config(authority, workstream="W12", role="WRITER"))
        self.assertIsNone(_dynamic_role_config(authority, workstream="W11", role="REVIEWER"))
        for invalid in ({}, {"provider_starting_branch": 123}, {"provider_starting_branch": ""}):
            with self.assertRaises(ValueError):
                _dynamic_provider_starting_branch(invalid)

    def test_complete_writer_task_contract_is_required(self):
        task = self.writer_task()
        self.assertEqual(_validate_task_spec(task, role="WRITER"), task)

        for missing in (
            "objective",
            "exact_baseline",
            "write_scope",
            "prohibited_scope",
            "validation",
            "evidence",
            "handoff",
            "stop_gate",
        ):
            with self.subTest(missing=missing):
                incomplete = self.writer_task()
                incomplete.pop(missing)
                with self.assertRaises(ValueError):
                    _validate_task_spec(incomplete, role="WRITER")

        empty_write = self.writer_task()
        empty_write["write_scope"] = []
        with self.assertRaises(ValueError):
            _validate_task_spec(empty_write, role="WRITER")

    def test_task_contract_is_schema_closed_type_strict_and_unambiguous(self):
        unknown = self.writer_task()
        unknown["extra_instructions"] = "widen scope"
        with self.assertRaises(ValueError):
            _validate_task_spec(unknown, role="WRITER")

        non_text_scope = self.writer_task()
        non_text_scope["write_scope"] = [123]
        with self.assertRaises(ValueError):
            _validate_task_spec(non_text_scope, role="WRITER")

        non_text_objective = self.writer_task()
        non_text_objective["objective"] = 123
        with self.assertRaises(ValueError):
            _validate_task_spec(non_text_objective, role="WRITER")

        conflicting_alias = self.writer_task()
        conflicting_alias["writeScope"] = ["other/**"]
        with self.assertRaises(ValueError):
            _validate_task_spec(conflicting_alias, role="WRITER")

        duplicate_validation = self.writer_task()
        duplicate_validation["tests"] = ["another check"]
        with self.assertRaises(ValueError):
            _validate_task_spec(duplicate_validation, role="WRITER")

    def test_reviewer_and_assurance_contracts_are_explicitly_read_only(self):
        for role in ("REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"):
            task = self.writer_task()
            task["write_scope"] = []
            self.assertEqual(_validate_task_spec(task, role=role), task)

            mutating = dict(task)
            mutating["write_scope"] = ["src/**"]
            with self.assertRaises(ValueError):
                _validate_task_spec(mutating, role=role)

    def test_validation_tests_alias_is_accepted_but_must_be_nonempty(self):
        task = self.writer_task()
        task.pop("validation")
        task["tests"] = ["python -m unittest"]
        self.assertEqual(_validate_task_spec(task, role="WRITER"), task)
        task["tests"] = []
        with self.assertRaises(ValueError):
            _validate_task_spec(task, role="WRITER")

    def test_duplicate_detection_is_exact_logical_lineage_marker_specific(self):
        inventory = [
            {
                "name": "sessions/other",
                "title": "RP01 OTHER WRITER [111111111111]",
                "_source_repository": "hamad933/Bayt-Style",
                "sourceStartingBranch": "main",
            },
            {
                "name": "sessions/exact",
                "title": "RP01 W11 WRITER [abcdef123456]",
                "_source_repository": "hamad933/Bayt-Style",
                "sourceStartingBranch": "main",
            },
            {
                "name": "sessions/wrong-ref",
                "title": "RP01 W11 WRITER [abcdef123456]",
                "_source_repository": "hamad933/Bayt-Style",
                "sourceStartingBranch": "work/other",
            },
        ]
        matches = _marker_matches(
            inventory,
            repository="hamad933/Bayt-Style",
            starting_branch="main",
            marker="abcdef123456",
        )
        self.assertEqual([item["name"] for item in matches], ["sessions/exact"])

    def test_prompt_is_deterministic_and_contains_only_governed_task_contract(self):
        first = self.writer_task()
        second = dict(reversed(list(first.items())))
        self.assertEqual(_task_prompt(first), _task_prompt(second))
        prompt = _task_prompt(first)
        self.assertIn("Do governed work", prompt)
        self.assertIn("DRAFT_PR_AND_EXACT_HEAD_CI", prompt)
        self.assertIn("Do not widen scope", prompt)

    def test_no_current_authority_is_safe_noop_before_provider_credentials(self):
        env = {
            "UES_CURRENT_AUTHORITY_JSON": "",
            "UES_AUTHORITY_TRANSPORT_ACTOR": "hamad933",
            "JULES_API_KEY": "",
            "GITHUB_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = run("RP01")
        self.assertEqual(result["result"], "INITIAL_LINEAGE_RUNTIME_NO_CURRENT_AUTHORITY")
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["safe_to_blind_retry"])

    def test_sessions_list_network_outage_is_structured_before_any_lineage_write(self):
        outage = NetworkError("provider network request failed", operation="jules.sessions.list")
        env = {"JULES_API_KEY": "test", "GITHUB_TOKEN": "test", "UES_AUTHORITY_TRANSPORT_ACTOR": "hamad933"}
        with patch.dict(os.environ, env, clear=False), patch(
            "ues.initial_lineage_runtime.load_current_authority_json", return_value=self.initial_authority()
        ), patch("ues.initial_lineage_runtime.build_live_state_store", return_value=object()), patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory", side_effect=outage
        ), patch("ues.initial_lineage_runtime.execute_initial_lineage_generation") as generation:
            result = run("RP02")

        generation.assert_not_called()
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["provider_read_operation"], "jules.sessions.list")
        self.assertEqual(result["provider_read_error_category"], "NETWORK_ERROR")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertEqual(result["retry_condition"], "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED")
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(result["authority_event_id"], "RP02-AUTH-OUTAGE")

    def test_sessions_list_rate_limit_outage_is_also_structured_pre_effect(self):
        outage = RateLimitError("provider rate limited", operation="jules.sessions.list")
        env = {"JULES_API_KEY": "test", "GITHUB_TOKEN": "test", "UES_AUTHORITY_TRANSPORT_ACTOR": "hamad933"}
        with patch.dict(os.environ, env, clear=False), patch(
            "ues.initial_lineage_runtime.load_current_authority_json", return_value=self.initial_authority()
        ), patch("ues.initial_lineage_runtime.build_live_state_store", return_value=object()), patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory", side_effect=outage
        ):
            result = run("RP02")
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertFalse(result["safe_to_blind_retry"])

    def test_non_inventory_provider_operation_is_not_reclassified_as_zero_effect(self):
        outage = NetworkError("provider network request failed", operation="jules.sessions.sendMessage")
        env = {"JULES_API_KEY": "test", "GITHUB_TOKEN": "test", "UES_AUTHORITY_TRANSPORT_ACTOR": "hamad933"}
        with patch.dict(os.environ, env, clear=False), patch(
            "ues.initial_lineage_runtime.load_current_authority_json", return_value=self.initial_authority()
        ), patch("ues.initial_lineage_runtime.build_live_state_store", return_value=object()), patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory", side_effect=outage
        ):
            with self.assertRaises(NetworkError):
                run("RP02")

    def test_cli_returns_fail_closed_exit_after_materializing_zero_effect_json(self):
        result = {
            "schema_version": "1.0",
            "project": "RP02",
            "route": "RP02",
            "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }
        with patch("ues.initial_lineage_runtime.run", return_value=result):
            rc = main(["RP02"])
        self.assertEqual(rc, _PROVIDER_READ_UNAVAILABLE_EXIT)


if __name__ == "__main__":
    unittest.main()
