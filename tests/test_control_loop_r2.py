from __future__ import annotations

import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ues.control_loop import run_shadow_cycle
from ues.identity import canonical_lane_id, lane_id_from_key, parse_lane_id
from ues.lifecycle import LifecycleState, ReviewOutcome, SourceBindingStatus
from ues.reconciliation import ActorBinding, ReviewBinding, WorkstreamBinding
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord

REPO = "hamad933/universal-execution-system"
NOW = datetime(2026, 8, 23, 21, 0, tzinfo=timezone.utc)
HEAD = "c" * 40
BASE = "b" * 40


def actor(role: str) -> ActorBinding:
    role = role.upper()
    return ActorBinding(
        role=role,
        provider="jules",
        session_id=f"{role.lower()}-session",
        task_id=f"{role.lower()}-task",
        lineage=f"{role.lower()}-lineage",
        source_repository=REPO,
        source_identity=f"sources/{REPO}",
        proof_status=SourceBindingStatus.PROVEN_EXPLICIT,
        evidence_id=f"binding-{role.lower()}",
    )


def binding(**overrides) -> WorkstreamBinding:
    values = {
        "project": "UES",
        "route": "INTERNAL:UES",
        "workstream": "W01",
        "role": "WRITER",
        "repo": REPO,
        "branch": "review/ues-auto-v2-r2-composition",
        "lifecycle_state": LifecycleState.WRITER_ACTIVE,
        "baseline_sha": BASE,
        "base_ref": "automation/portfolio-control-plane-v2",
        "task_budget_class": "NO_NEW_TASK_REQUIRED",
        "last_activity_at": NOW,
        "writer_lineage": "writer-lineage",
        "reviewer_lineage": "reviewer-lineage",
        "actor_bindings": (actor("WRITER"), actor("REVIEWER")),
        "scope_identity": "integration:r2",
        "head_sha": HEAD,
        "pr_number": 16,
    }
    values.update(overrides)
    return WorkstreamBinding(**values)


class CanonicalLaneIdentityTests(unittest.TestCase):
    def test_scalar_lane_id_is_reversible_and_complete(self):
        lane_id = canonical_lane_id("UES", "PERSONAL:UES", "W01")
        self.assertEqual(parse_lane_id(lane_id), ("UES", "PERSONAL:UES", "W01"))
        self.assertEqual(
            lane_id_from_key(("UES", "PERSONAL:UES", "W01")),
            lane_id,
        )

    def test_same_bare_workstream_across_projects_cannot_collide(self):
        gs = canonical_lane_id("GS", "PERSONAL:GS", "W01")
        cep = canonical_lane_id("CEP", "PERSONAL:CEP", "W01")
        self.assertNotEqual(gs, cep)

    def test_delimiter_content_is_encoded_not_ambiguous(self):
        first = canonical_lane_id("A|B", "PERSONAL:A", "W01")
        second = canonical_lane_id("A", "B|PERSONAL:A", "W01")
        self.assertNotEqual(first, second)
        self.assertEqual(parse_lane_id(first)[0], "A|B")

    def test_missing_identity_component_fails_closed(self):
        with self.assertRaises(ValueError):
            canonical_lane_id("UES", "", "W01")


class ShadowControlLoopTests(unittest.TestCase):
    def test_external_effect_is_observed_but_never_dispatched(self):
        result = run_shadow_cycle([binding()])
        self.assertEqual(result["activation_mode"], "SHADOW")
        self.assertFalse(result["mutation_allowed"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["tasks_or_sessions_created"], 0)
        self.assertEqual(result["external_effect_candidates"][0]["action"], "PUBLISH_CANDIDATE")
        self.assertEqual(result["lanes"][0]["shadow_route"], "OBSERVE_EXTERNAL_EFFECT_CANDIDATE")
        self.assertFalse(result["lanes"][0]["external_effect_dispatched"])

    def test_parent_review_control_signal_remains_non_mutating(self):
        reviewed = ReviewBinding(
            review_id="review-1",
            reviewed_sha=HEAD,
            reviewer_lineage="reviewer-lineage",
            source_repository=REPO,
            evidence_classification="EXACT_SHA_REVIEW",
            outcome=ReviewOutcome.PASS,
        )
        result = run_shadow_cycle(
            [binding(lifecycle_state=LifecycleState.REVIEW_RESULT, review=reviewed)]
        )
        lane = result["lanes"][0]
        self.assertEqual(lane["semantic_action"], "REQUEST_PARENT_REVIEW")
        self.assertEqual(lane["required_capability"], "CONTROL_SIGNAL")
        self.assertEqual(lane["shadow_route"], "CONTROL_SIGNAL")
        self.assertFalse(lane["mutation_allowed"])

    def test_runtime_canary_observation_does_not_escape_shadow(self):
        lane_id = canonical_lane_id("UES", "INTERNAL:UES", "W01")
        with tempfile.TemporaryDirectory() as tmp:
            store = DeterministicFileStateStore(Path(tmp) / "runtime.json", clock=lambda: NOW)
            store.initialize()
            store.compare_and_swap_workstream(
                lane_id,
                0,
                WorkstreamRuntimeRecord(
                    lane_id=lane_id,
                    project="UES",
                    route="INTERNAL:UES",
                    workstream_id="W01",
                    activation_mode="CANARY",
                ),
            )
            result = run_shadow_cycle([binding()], state_store=store)

        lane = result["lanes"][0]
        self.assertEqual(lane["runtime_state_status"], "OK")
        self.assertEqual(lane["runtime_observed_activation_mode"], "CANARY")
        self.assertEqual(lane["effective_activation_mode"], "SHADOW")
        self.assertFalse(lane["mutation_allowed"])
        self.assertEqual(result["external_effects_dispatched"], 0)

    def test_blocked_lane_does_not_freeze_independent_lane(self):
        blocked = binding(
            workstream="BLOCKED",
            project=None,
        )
        ready = binding(workstream="READY")
        result = run_shadow_cycle([blocked, ready])
        ready_lane = next(item for item in result["lanes"] if item["workstream"] == "READY")
        self.assertFalse(ready_lane["blocked"])
        self.assertIn(ready_lane["lane_id"], result["watchdog"]["executable_lanes"])


class PackagingTests(unittest.TestCase):
    def test_setuptools_discovers_provider_subpackages(self):
        payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        include = payload["tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertEqual(include, ["ues*"])

        import ues.providers  # noqa: F401


if __name__ == "__main__":
    unittest.main()
