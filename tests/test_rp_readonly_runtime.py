from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from ues import provider_observer, provider_observer_runtime
from ues.rp_readonly_runtime import (
    RP_NAMES,
    RP_PROJECTS,
    _load_rp_adapter,
    _with_rp_observer_projects,
    lifecycle_health,
)


class RPReadOnlyRuntimeTests(unittest.TestCase):
    def test_exact_rp_project_set_and_adapters_are_shadow(self):
        self.assertEqual(RP_NAMES, {"RP01", "RP02", "RP03", "RP04"})
        for project in sorted(RP_NAMES):
            with self.subTest(project=project):
                adapter = _load_rp_adapter(project)
                self.assertEqual(adapter["project"], project)
                self.assertEqual(adapter["route"], project)
                self.assertEqual(adapter["activation"]["default_mode"], "SHADOW")
                self.assertFalse(adapter["activation"]["mutation_allowed"])
                self.assertFalse(adapter["task_budget"]["automatic_new_task_creation"])

    def test_provider_composition_temporarily_scopes_shared_observer_to_rps(self):
        original_observer = provider_observer.PROJECTS
        original_runtime = provider_observer_runtime.PROJECTS

        def inspect():
            self.assertEqual(provider_observer.PROJECTS, RP_PROJECTS)
            self.assertEqual(provider_observer_runtime.PROJECTS, RP_PROJECTS)
            return {"result": "TEST", "provider_mutation_performed": False}

        result = _with_rp_observer_projects(inspect)
        self.assertEqual(provider_observer.PROJECTS, original_observer)
        self.assertEqual(provider_observer_runtime.PROJECTS, original_runtime)
        self.assertEqual(result["project_set"], ["RP01", "RP02", "RP03", "RP04"])
        self.assertEqual(result["rp_runtime_mode"], "READ_ONLY_SHADOW")
        self.assertFalse(result["provider_mutation_performed"])

    def test_lifecycle_health_uses_observation_backed_no_effect_path(self):
        expected = {
            "project": "RP03",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "current_authority_loaded": False,
            "provider_live_read_performed": False,
        }
        with patch.dict("os.environ", {"UES_CURRENT_AUTHORITY_JSON": ""}, clear=False), patch(
            "ues.rp_readonly_runtime.run_observation_backed_no_effect_health",
            return_value=expected,
        ) as health:
            result = lifecycle_health("RP03")

        health.assert_called_once()
        adapter = health.call_args.args[0]
        self.assertEqual(adapter["project"], "RP03")
        self.assertIsNone(health.call_args.kwargs["authority"])
        self.assertEqual(result["project"], "RP03")
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["provider_live_read_performed"])
        self.assertFalse(result["runtime_wrapper_grants_authority"])

    def test_readonly_health_rejects_authority_transport(self):
        with patch.dict("os.environ", {"UES_CURRENT_AUTHORITY_JSON": '{"current":true}'}, clear=False):
            with self.assertRaises(ValueError):
                lifecycle_health("RP01")

    def test_workflow_is_authority_neutral_and_has_no_provider_mutation_command(self):
        text = Path(".github/workflows/ues-rp-readonly-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("project: [RP01, RP02, RP03, RP04]", text)
        self.assertIn('UES_CURRENT_AUTHORITY_JSON: ""', text)
        self.assertIn("observe-provider", text)
        self.assertIn("lifecycle-health", text)
        self.assertIn("audit-provider", text)
        self.assertNotIn("create-session", text.lower())
        self.assertNotIn("send-message", text.lower())
        self.assertNotIn("repository_dispatch", text)
        self.assertNotIn("pull_request_target", text)


if __name__ == "__main__":
    unittest.main()
