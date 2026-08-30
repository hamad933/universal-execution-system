from __future__ import annotations

import unittest

from ues import provider_observer, provider_observer_runtime
from ues.rp03_provider_observer_canary import RP03_PROJECT, configure_rp03_scope


class Rp03ProviderObserverCanaryTests(unittest.TestCase):
    def test_configure_rp03_scope_is_exact_and_read_only_registry_only(self) -> None:
        original_observer = provider_observer.PROJECTS
        original_runtime = provider_observer_runtime.PROJECTS
        try:
            scope = configure_rp03_scope()
            self.assertEqual(scope, (RP03_PROJECT,))
            self.assertEqual(provider_observer.PROJECTS, scope)
            self.assertEqual(provider_observer_runtime.PROJECTS, scope)
            self.assertEqual(provider_observer._project_for_repository("hamad933/BOOKING-SERVICES"), RP03_PROJECT)
            self.assertIsNone(provider_observer._project_for_repository("hamad933/GS-2"))
        finally:
            provider_observer.PROJECTS = original_observer
            provider_observer_runtime.PROJECTS = original_runtime


if __name__ == "__main__":
    unittest.main()
