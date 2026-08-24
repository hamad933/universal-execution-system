from __future__ import annotations

import json
from pathlib import Path
import unittest


PROJECTS = {
    "RP01": "hamad933/Bayt-Style",
    "RP02": "hamad933/Enterprise-Operations-Control",
    "RP03": "hamad933/BOOKING-SERVICES",
    "RP04": "hamad933/Real-Estate-Assets-Control-",
}


class RPControlAdapterTests(unittest.TestCase):
    def _adapter(self, project: str) -> dict:
        return json.loads(Path(f"adapters/{project.lower()}.json").read_text(encoding="utf-8"))

    def test_identity_truth_and_shadow_defaults(self):
        for project, repository in PROJECTS.items():
            with self.subTest(project=project):
                adapter = self._adapter(project)
                self.assertEqual(adapter["project"], project)
                self.assertEqual(adapter["route"], project)
                self.assertEqual(adapter["repository"], repository)
                self.assertEqual(adapter["activation"]["default_mode"], "SHADOW")
                self.assertFalse(adapter["activation"]["mutation_allowed"])
                self.assertFalse(adapter["activation"]["runtime_mode_is_authority"])
                self.assertEqual(adapter["truth_owners"]["governed_state"], "DRIVE")
                self.assertEqual(adapter["truth_owners"]["technical_state"], "GITHUB")
                self.assertEqual(adapter["truth_owners"]["provider_state"], "PROVIDER")

    def test_current_authority_owns_dynamic_lineages_and_generation(self):
        for project in PROJECTS:
            with self.subTest(project=project):
                adapter = self._adapter(project)
                transport = adapter["authority_transport"]
                runtime = adapter["lineage_runtime"]
                self.assertEqual(transport["canonical_source"], "DRIVE_CURRENT_STATE")
                self.assertTrue(transport["bounded_expiry_required"])
                self.assertFalse(transport["transport_is_truth_owner"])
                self.assertEqual(transport["controller_actor_allowlist"], ["hamad933"])
                self.assertTrue(runtime["current_authority_required_for_new_generation"])
                self.assertTrue(runtime["dynamic_governed_lineages_allowed"])
                self.assertEqual(runtime["workstreams"], {})
                self.assertTrue(runtime["reuse_same_session_first"])
                self.assertTrue(runtime["replacement_is_next_generation_only"])
                self.assertTrue(runtime["unbound_never_implies_replacement"])
                self.assertTrue(runtime["replacement_requires_governed_cause"])

    def test_stable_task_defaults_remain_parent_only_and_fail_closed(self):
        for project in PROJECTS:
            with self.subTest(project=project):
                adapter = self._adapter(project)
                budget = adapter["task_budget"]
                self.assertEqual(budget["new_task_authority"], "PARENT_ONLY")
                self.assertEqual(budget["unknown_lifetime_capacity"], "DENY")
                self.assertFalse(budget["automatic_new_task_creation"])
                self.assertTrue(budget["current_ceiling_must_be_resolved_at_runtime"])
                self.assertTrue(budget["runtime_budget_preflight_required"])
                self.assertNotIn("ceiling", budget)
                self.assertNotIn("reserve_target", budget)
                dispatch = adapter["workflow_dispatch_policy"]
                self.assertFalse(dispatch["enabled"])
                self.assertFalse(dispatch["allow_arbitrary_workflow"])
                self.assertEqual(dispatch["workflows"], {})

    def test_no_mutable_project_or_provider_truth_is_committed(self):
        for project in PROJECTS:
            with self.subTest(project=project):
                raw = Path(f"adapters/{project.lower()}.json").read_text(encoding="utf-8")
                self.assertNotRegex(raw, r"\b[0-9a-f]{40}\b")
                self.assertNotRegex(raw, r"\b[0-9a-f]{64}\b")
                self.assertNotIn('"current_sha":', raw)
                self.assertNotIn('"current_candidate_sha":', raw)
                self.assertNotIn("session_id", raw)
                self.assertNotIn("authority_event_id", raw)
                self.assertNotIn("api_key", raw.lower())

    def test_safety_prohibitions_and_unclassified_waiting_are_preserved(self):
        required = {
            "MERGE", "RELEASE", "DEPLOY", "PRODUCT_PUBLICATION", "FORCE_PUSH",
            "TEST_WEAKENING", "GUESSED_SESSION_OWNERSHIP", "BLIND_WRITE_RETRY",
            "UNGUARDED_AUTOMATIC_NEW_JULES_TASK",
        }
        for project in PROJECTS:
            with self.subTest(project=project):
                adapter = self._adapter(project)
                self.assertTrue(required <= set(adapter["prohibitions"]))
                classifier = adapter["waiting_classifier"]
                self.assertEqual(classifier["rules"], [])
                self.assertEqual(classifier["unmatched"], "UNCLASSIFIED")
                self.assertFalse(classifier["keyword_shortcuts_allowed"])


if __name__ == "__main__":
    unittest.main()
