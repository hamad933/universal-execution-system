from __future__ import annotations

import unittest

from ues.generation_transition import assess_generation_transition
from ues.lifecycle_runtime_v2 import (
    _generation_preconditions,
    _policy_exact_baseline,
    _replacement_prompt,
    _structured_handoff_recovery_ready,
)
from ues.structured_handoff import build_exact_review_handoff_instructions


class FakeGitHub:
    def __init__(self, exact: bool = True):
        self.exact = exact
        self.calls: list[tuple[str, str, str, str]] = []

    def verify_exact_head(self, owner: str, repo: str, ref: str, sha: str):
        self.calls.append((owner, repo, ref, sha))
        return {"exact_head_match": self.exact}


class StructuredHandoffRecoveryTests(unittest.TestCase):
    SHA = "a" * 40

    def test_exact_review_handoff_prebinds_candidate_and_reviewed_sha(self):
        prompt = build_exact_review_handoff_instructions("REVIEWER", "RP02-IPA-S03-001", self.SHA)
        self.assertIn(f'"candidate_sha": "{self.SHA}"', prompt)
        self.assertIn(f'"reviewed_sha": "{self.SHA}"', prompt)
        self.assertNotIn('"reviewed_sha": null', prompt)

    def test_replacement_review_prompt_is_exact_sha_bound(self):
        prompt = _replacement_prompt(
            "ASSURANCE",
            "RP03-IPA-S01-001",
            {"replacement_prompt": "Recover structured handoff for {workstream} at {current_sha}"},
            self.SHA,
        )
        self.assertIsNotNone(prompt)
        self.assertIn(self.SHA, prompt or "")
        self.assertIn(f'"reviewed_sha": "{self.SHA}"', prompt or "")

    def test_explicit_exact_baseline_supports_frozen_main_without_pr(self):
        policy = {"exact_baseline": f"main@{self.SHA}"}
        self.assertEqual(_policy_exact_baseline(policy), ("main", self.SHA))
        github = FakeGitHub(exact=True)
        ref, sha, source_proven, ref_proven = _generation_preconditions(
            github=github,
            repository="owner/repo",
            pr_state={"pr": None, "current_sha": None},
            policy=policy,
            source_proven=True,
        )
        self.assertEqual((ref, sha), ("main", self.SHA))
        self.assertTrue(source_proven)
        self.assertTrue(ref_proven)
        self.assertEqual(github.calls, [("owner", "repo", "main", self.SHA)])

    def test_explicit_baseline_fails_closed_on_conflicting_pr_identity(self):
        github = FakeGitHub(exact=True)
        ref, sha, _, ref_proven = _generation_preconditions(
            github=github,
            repository="owner/repo",
            pr_state={
                "pr": {"head_ref": "other"},
                "current_sha": "b" * 40,
            },
            policy={"exact_baseline": f"main@{self.SHA}"},
            source_proven=True,
        )
        self.assertEqual((ref, sha), ("main", self.SHA))
        self.assertFalse(ref_proven)
        self.assertEqual(github.calls, [])

    def test_structured_recovery_requires_proven_completed_unstructured_existing_lineage(self):
        state = {
            "generation": 1,
            "session_fingerprint": "f" * 64,
            "unknown_write_state": False,
            "action_in_flight": False,
        }
        self.assertTrue(
            _structured_handoff_recovery_ready(
                role="REVIEWER",
                binding={"status": "PROVEN", "provider_state": "COMPLETED"},
                handoff=None,
                state_snapshot=state,
            )
        )
        self.assertFalse(
            _structured_handoff_recovery_ready(
                role="REVIEWER",
                binding={"status": "PROVEN", "provider_state": "IN_PROGRESS"},
                handoff=None,
                state_snapshot=state,
            )
        )
        self.assertFalse(
            _structured_handoff_recovery_ready(
                role="REVIEWER",
                binding={"status": "PROVEN", "provider_state": "COMPLETED"},
                handoff={"verdict": "PASS"},
                state_snapshot=state,
            )
        )

    def test_structured_recovery_is_review_only_and_can_recover_after_closed_pr(self):
        policy = {
            "necessary_generation_authorized": True,
            "generation_effect_authorized": True,
            "generation_budget_safe": True,
            "budget": {"hard_ceiling_reached": False},
        }
        reviewer = assess_generation_transition(
            project="RP02",
            route="RP02",
            workstream="RP02-IPA-S03-001",
            role="REVIEWER",
            current_generation=1,
            predecessor_session_fingerprint="f" * 64,
            candidate_sha=self.SHA,
            replacement_cause="STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
            work_remaining=False,
            current_policy=policy,
            active_duplicate_absent=True,
            unknown_write_state=False,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
            replacement_task_spec_ready=True,
        )
        self.assertTrue(reviewer["allowed"])

        writer = assess_generation_transition(
            project="RP02",
            route="RP02",
            workstream="RP02-IPA-S03-001",
            role="WRITER",
            current_generation=1,
            predecessor_session_fingerprint="f" * 64,
            candidate_sha=self.SHA,
            replacement_cause="STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
            work_remaining=False,
            current_policy=policy,
            active_duplicate_absent=True,
            unknown_write_state=False,
            exact_repository_binding=True,
            exact_starting_ref_binding=True,
            replacement_task_spec_ready=True,
        )
        self.assertFalse(writer["allowed"])
        self.assertIn("STRUCTURED_HANDOFF_RECOVERY_REQUIRES_REVIEW_ROLE", writer["failures"])


if __name__ == "__main__":
    unittest.main()
