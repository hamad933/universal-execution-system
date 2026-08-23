"""Production-backed replay gate for a composed A-D candidate.

Normal test discovery skips production execution. With UES_CONTROL_PLANE_INTEGRATION=1,
every fixture is executed through actual production modules and normalized back to the
fixture contract. Missing bindings and wrong behavior are hard failures.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import unittest

from tests.control_plane.production_adapters import IntegrationBindingUnavailable, ProductionReplayAdapter
from tests.control_plane.protocols import EXPECTED_PRODUCTION_MODULES
from tests.control_plane.replay_harness import canonical, load_corpus

INTEGRATION_ENABLED = os.getenv("UES_CONTROL_PLANE_INTEGRATION") == "1"

class IntegrationHarnessShapeTests(unittest.TestCase):
    def test_every_fixture_kind_has_a_production_adapter(self):
        adapter = ProductionReplayAdapter()
        missing = sorted({case.kind for case in load_corpus() if not callable(getattr(adapter, f"_eval_{case.kind}", None))})
        self.assertEqual(missing, [])

    def test_production_adapter_does_not_import_reference_oracle(self):
        source = inspect.getsource(importlib.import_module("tests.control_plane.production_adapters"))
        tree = ast.parse(source)
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "tests.control_plane.replay_harness":
                    forbidden.append(f"from:{module}")
                if any(alias.name == "ReferenceOracle" for alias in node.names):
                    forbidden.append("name:ReferenceOracle")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tests.control_plane.replay_harness" or alias.name.endswith(".ReferenceOracle"):
                        forbidden.append(f"import:{alias.name}")
        self.assertEqual(forbidden, [])

@unittest.skipUnless(INTEGRATION_ENABLED, "set UES_CONTROL_PLANE_INTEGRATION=1 on composed A-D candidate")
class ProductionBackedReplayTests(unittest.TestCase):
    def test_required_modules_are_importable(self):
        failures=[]
        for name in EXPECTED_PRODUCTION_MODULES:
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "INTEGRATION_MODULE_FAILURES:\n" + "\n".join(failures))

    def test_entire_replay_corpus_matches_actual_production_behavior(self):
        adapter=ProductionReplayAdapter()
        failures=[]
        for case in load_corpus():
            try:
                actual=adapter.evaluate(case)
            except IntegrationBindingUnavailable as exc:
                failures.append(f"{case.scenario_id} BINDING_UNAVAILABLE {exc}")
                continue
            except Exception as exc:
                failures.append(f"{case.scenario_id} EXECUTION_ERROR {type(exc).__name__}: {exc}")
                continue
            if canonical(actual) != canonical(case.expected):
                failures.append(f"{case.scenario_id} MISMATCH actual={canonical(actual)} expected={canonical(case.expected)}")
        self.assertEqual(failures, [], "PRODUCTION_REPLAY_FAILURES:\n" + "\n".join(failures))

if __name__ == "__main__":
    unittest.main()
