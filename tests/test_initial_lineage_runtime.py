from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.initial_lineage_runtime import (
    _dynamic_role_config,
    _marker_matches,
    _parse_exact_baseline,
    _parse_lane_key,
    _task_prompt,
    run,
)


class InitialLineageRuntimeTests(unittest.TestCase):
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
        self.assertIsNotNone(_dynamic_role_config(authority, workstream="W11", role="WRITER"))
        self.assertIsNone(_dynamic_role_config(authority, workstream="W12", role="WRITER"))
        self.assertIsNone(_dynamic_role_config(authority, workstream="W11", role="REVIEWER"))

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
        first = {
            "objective": "Do governed work",
            "exact_baseline": "main@" + "a" * 40,
            "write_scope": ["src/**"],
            "prohibited_scope": [],
            "validation": ["unit"],
            "evidence": ["exact-head-ci"],
            "handoff": "Return evidence",
            "stop_gate": "DRAFT_PR",
        }
        second = dict(reversed(list(first.items())))
        self.assertEqual(_task_prompt(first), _task_prompt(second))
        prompt = _task_prompt(first)
        self.assertIn("Do governed work", prompt)
        self.assertIn("DRAFT_PR", prompt)
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


if __name__ == "__main__":
    unittest.main()
