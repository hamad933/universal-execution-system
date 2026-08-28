from __future__ import annotations

import unittest

from ues.initial_lineage_runtime import _task_prompt, _validate_task_spec
from ues.structured_handoff import END_MARKER, START_MARKER


class InitialLineagePromptHandoffTests(unittest.TestCase):
    def task(self, *, write_scope: list[str] | None = None, sha: str | None = None) -> dict[str, object]:
        exact_sha = sha or ("a" * 40)
        return {
            "objective": "Perform the governed task",
            "exact_baseline": "main@" + exact_sha,
            "write_scope": [] if write_scope is None else write_scope,
            "prohibited_scope": ["deploy/**"],
            "validation": ["python -m unittest"],
            "evidence": ["exact-head evidence"],
            "handoff": "Return structured evidence",
            "stop_gate": "RETURN_TO_PARENT",
        }

    def test_reviewer_prompt_requires_machine_handoff_bound_to_exact_reviewed_sha(self):
        sha = "b" * 40
        prompt = _task_prompt(
            self.task(sha=sha),
            role="REVIEWER",
            workstream="RP02-IPA-S03-001",
        )
        self.assertIn(START_MARKER, prompt)
        self.assertIn(END_MARKER, prompt)
        self.assertIn('"role": "REVIEWER"', prompt)
        self.assertIn('"workstream": "RP02-IPA-S03-001"', prompt)
        self.assertIn(f'"candidate_sha": "{sha}"', prompt)
        self.assertIn(f'"reviewed_sha": "{sha}"', prompt)
        self.assertNotIn('"reviewed_sha": null', prompt)

    def test_reviewer_prompt_declares_supplied_spec_as_authoritative_workstream_contract(self):
        sha = "d" * 40
        prompt = _task_prompt(
            self.task(sha=sha),
            role="REVIEWER",
            workstream="RP02-IPA-S05-001",
        )
        self.assertIn(
            "supplied task specification IS the authoritative Parent Controller Workstream Contract",
            prompt,
        )
        self.assertIn(
            "Do not require or search for a second repository-local Workstream Contract",
            prompt,
        )
        self.assertIn(f'"exact_baseline":"main@{sha}"', prompt)
        self.assertIn('"write_scope":[]', prompt)
        self.assertIn("fail closed and report the evidence boundary", prompt)

    def test_task_contract_validation_fails_closed_when_required_field_is_missing(self):
        task = self.task()
        task.pop("evidence")
        with self.assertRaisesRegex(ValueError, "task_spec.evidence is required"):
            _validate_task_spec(task, role="REVIEWER")

    def test_final_assurance_uses_assurance_role_and_exact_reviewed_sha(self):
        sha = "c" * 40
        prompt = _task_prompt(
            self.task(sha=sha),
            role="FINAL_ASSURANCE",
            workstream="RP04-S07-FINAL",
        )
        self.assertIn('"role": "ASSURANCE"', prompt)
        self.assertIn(f'"candidate_sha": "{sha}"', prompt)
        self.assertIn(f'"reviewed_sha": "{sha}"', prompt)

    def test_writer_prompt_still_requires_machine_handoff_without_fake_reviewed_sha(self):
        prompt = _task_prompt(
            self.task(write_scope=["src/**"]),
            role="WRITER",
            workstream="RP01-S09-CORRECTION",
        )
        self.assertIn(START_MARKER, prompt)
        self.assertIn(END_MARKER, prompt)
        self.assertIn('"role": "WRITER"', prompt)
        self.assertIn('"reviewed_sha": null', prompt)

    def test_role_and_workstream_must_be_supplied_together(self):
        with self.assertRaises(ValueError):
            _task_prompt(self.task(), role="REVIEWER")
        with self.assertRaises(ValueError):
            _task_prompt(self.task(), workstream="RP02-IPA-S03-001")


if __name__ == "__main__":
    unittest.main()
