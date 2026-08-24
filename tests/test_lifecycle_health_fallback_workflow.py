from pathlib import Path
import unittest


class LifecycleHealthFallbackWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = Path('.github/workflows/ues-live-runtime-foundation.yml').read_text(encoding='utf-8')

    def test_fallback_runs_only_from_the_proven_independent_schedule(self) -> None:
        self.assertIn('scheduled-lifecycle-health-fallback:', self.workflow)
        self.assertIn("if: github.event_name == 'schedule'", self.workflow)
        self.assertIn('needs: scheduled-provider-observer', self.workflow)
        self.assertIn('matrix:\n        project: [CEP, GS]', self.workflow)

    def test_fallback_serializes_with_primary_project_lifecycle_lane(self) -> None:
        self.assertIn('group: ues-lineage-lifecycle-${{ matrix.project }}', self.workflow)
        self.assertIn('cancel-in-progress: false', self.workflow)

    def test_fallback_is_authority_neutral_and_uses_observed_runtime(self) -> None:
        self.assertIn('UES_CURRENT_AUTHORITY_JSON: ""', self.workflow)
        self.assertIn('UES_WAKEUP_EVENT_SOURCE: provider-observer-schedule-fallback', self.workflow)
        self.assertIn('run: python -m ues.lifecycle_runtime_observed ${{ matrix.project }}', self.workflow)
        self.assertNotIn('repository_dispatch: ues-lifecycle-wakeup', self.workflow)

    def test_existing_provider_observer_remains_read_only(self) -> None:
        self.assertIn('Observe Jules GS/CEP sessions read-only with durable health', self.workflow)
        self.assertIn('run: python -m ues.provider_observer_runtime observe', self.workflow)


if __name__ == '__main__':
    unittest.main()
