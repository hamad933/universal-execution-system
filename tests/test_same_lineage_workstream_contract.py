from __future__ import annotations

import json
import unittest

from ues.binding_safe_generation import (
    _replacement_review_contract,
    execute_binding_safe_generation,
)


class SameLineageWorkstreamContractTests(unittest.TestCase):
    def contract_prompt(self, *, sha: str = "a" * 40, workstream: str = "RP04-IPA-S02-001") -> str:
        contract = {
            "objective": "Independently review one governed page surface and return a structured handoff.",
            "exact_baseline": f"main@{sha}",
            "role": "REVIEWER",
            "logical_lineage": workstream,
            "write_scope": [],
            "prohibited_scope": ["repository mutation", "merge", "release"],
            "validation": ["inspect exact reviewed SHA", "verify applicable page requirements"],
            "evidence": ["exact-SHA repository evidence", "governed review packet"],
            "handoff": "Return canonical UES_HANDOFF_V1.",
            "stop_gate": "Stop after one exact-SHA independent review handoff.",
        }
        return "PARENT_CONTROLLER_WORKSTREAM_CONTRACT_V1=" + json.dumps(
            contract, sort_keys=True, separators=(",", ":")
        ) + "\n\nPerform the bounded recovery review."

    def test_complete_exact_contract_is_accepted(self):
        sha = "b" * 40
        prompt = self.contract_prompt(sha=sha)
        contract = _replacement_review_contract(
            prompt,
            workstream="RP04-IPA-S02-001",
            role="REVIEWER",
            candidate_sha=sha,
        )
        self.assertIsNotNone(contract)
        self.assertEqual(contract["write_scope"], [])
        self.assertEqual(contract["role"], "REVIEWER")

    def test_missing_contract_is_rejected(self):
        self.assertIsNone(
            _replacement_review_contract(
                "Recover the missing structured handoff.",
                workstream="RP04-IPA-S02-001",
                role="REVIEWER",
                candidate_sha="a" * 40,
            )
        )

    def test_wrong_lineage_or_sha_is_rejected(self):
        prompt = self.contract_prompt(sha="c" * 40)
        self.assertIsNone(
            _replacement_review_contract(
                prompt,
                workstream="RP04-IPA-S03-001",
                role="REVIEWER",
                candidate_sha="c" * 40,
            )
        )
        self.assertIsNone(
            _replacement_review_contract(
                prompt,
                workstream="RP04-IPA-S02-001",
                role="REVIEWER",
                candidate_sha="d" * 40,
            )
        )

    def test_reviewer_generation_fails_before_state_or_provider_without_contract(self):
        class MustNotBeUsed:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected access: {name}")

        result = execute_binding_safe_generation(
            MustNotBeUsed(),
            MustNotBeUsed(),
            project="RP04",
            route="RP04",
            workstream="RP04-IPA-S02-001",
            role="REVIEWER",
            prompt="Recover structured handoff without a Workstream Contract.",
            title="RP04 reviewer recovery",
            source_name="sources/example",
            starting_branch="main",
            repository="hamad933/Real-Estate-Assets-Control-",
            authority_event_id="RP04-AUTH-TEST",
            current_policy={},
            replacement_cause="STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
            candidate_sha="a" * 40,
            work_remaining=True,
            active_duplicate_absent=True,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
        )
        self.assertEqual(result["decision"], "NEXT_GENERATION_WORKSTREAM_CONTRACT_REQUIRED")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_writer_generation_keeps_existing_path(self):
        # This regression only gates Reviewer/Assurance replacement generations.
        # Writer generation still reaches ordinary state preflight rather than the
        # new review-contract decision.
        class MissingStore:
            def read_workstream(self, lane_id):
                class Read:
                    status = "MISSING"
                    record = None
                return Read()

        with self.assertRaises(Exception):
            execute_binding_safe_generation(
                MissingStore(),
                object(),
                project="RP04",
                route="RP04",
                workstream="RP04-WRITER-001",
                role="WRITER",
                prompt="bounded writer recovery",
                title="writer",
                source_name="sources/example",
                starting_branch="main",
                repository="hamad933/Real-Estate-Assets-Control-",
                authority_event_id="RP04-AUTH-TEST",
                current_policy={},
                replacement_cause="TERMINAL_CONTEXT_EXHAUSTED",
                candidate_sha="a" * 40,
                work_remaining=True,
                active_duplicate_absent=True,
                exact_repository_binding=True,
                exact_starting_ref_binding=True,
            )


if __name__ == "__main__":
    unittest.main()
