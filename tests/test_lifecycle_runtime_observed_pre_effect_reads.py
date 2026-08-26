from __future__ import annotations

import unittest

from ues.lifecycle_runtime_observed import (
    _PRE_EFFECT_PROVIDER_READ_OPERATIONS,
    _is_pre_effect_provider_read_failure,
)


class _ProviderReadError(RuntimeError):
    def __init__(self, operation: str):
        super().__init__(operation)
        self.operation = operation


class LifecycleRuntimeObservedPreEffectReadTests(unittest.TestCase):
    def test_session_inventory_list_is_pre_effect(self):
        self.assertIn("jules.sessions.list", _PRE_EFFECT_PROVIDER_READ_OPERATIONS)
        self.assertTrue(_is_pre_effect_provider_read_failure(_ProviderReadError("jules.sessions.list")))

    def test_session_hydration_get_is_pre_effect(self):
        self.assertIn("jules.sessions.get", _PRE_EFFECT_PROVIDER_READ_OPERATIONS)
        self.assertTrue(_is_pre_effect_provider_read_failure(_ProviderReadError("jules.sessions.get")))

    def test_activity_reads_are_not_reclassified(self):
        self.assertNotIn("jules.activities.list", _PRE_EFFECT_PROVIDER_READ_OPERATIONS)
        self.assertFalse(_is_pre_effect_provider_read_failure(_ProviderReadError("jules.activities.list")))

    def test_provider_write_operation_is_not_reclassified(self):
        self.assertFalse(_is_pre_effect_provider_read_failure(_ProviderReadError("jules.sessions.create")))


if __name__ == "__main__":
    unittest.main()
