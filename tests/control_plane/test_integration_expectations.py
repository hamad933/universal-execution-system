"""Integration expectations for Domains A-D.

Default discovery skips these assertions so the synthetic corpus can be validated
on the frozen baseline. Set UES_CONTROL_PLANE_INTEGRATION=1 after composing A-D
onto a candidate tree to turn missing production modules into hard failures.
"""

from __future__ import annotations

import importlib
import os
import unittest

from tests.control_plane.protocols import EXPECTED_PRODUCTION_MODULES


INTEGRATION_ENABLED = os.getenv("UES_CONTROL_PLANE_INTEGRATION") == "1"


@unittest.skipUnless(INTEGRATION_ENABLED, "set UES_CONTROL_PLANE_INTEGRATION=1 on composed A-D candidate")
class ProductionIntegrationExpectationTests(unittest.TestCase):
    def _require_module(self, module_name: str):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            self.fail(f"INTEGRATION_EXPECTATION_MISSING: {module_name}: {exc}")

    def test_domain_a_modules(self):
        self._require_module("ues.lifecycle")
        self._require_module("ues.reconciliation")

    def test_domain_b_modules(self):
        self._require_module("ues.providers.jules")
        self._require_module("ues.providers.github")
        self._require_module("ues.recovery")
        self._require_module("ues.failures")

    def test_domain_c_modules(self):
        self._require_module("ues.routing")
        self._require_module("ues.watchdog")
        self._require_module("ues.task_budget")
        self._require_module("ues.metrics")

    def test_domain_d_modules(self):
        self._require_module("ues.state_store")
        self._require_module("ues.idempotency")
        self._require_module("ues.operation_records")
        self._require_module("ues.transaction")

    def test_expected_module_inventory_is_stable(self):
        self.assertEqual(
            EXPECTED_PRODUCTION_MODULES,
            (
                "ues.lifecycle",
                "ues.reconciliation",
                "ues.providers.jules",
                "ues.providers.github",
                "ues.routing",
                "ues.watchdog",
                "ues.task_budget",
                "ues.metrics",
                "ues.state_store",
                "ues.recovery",
                "ues.failures",
                "ues.idempotency",
                "ues.operation_records",
                "ues.transaction",
            ),
        )


if __name__ == "__main__":
    unittest.main()
