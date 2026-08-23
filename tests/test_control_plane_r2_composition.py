from __future__ import annotations

import unittest

from tests.control_plane.production_adapters import (
    IntegrationBindingUnavailable,
    ProductionReplayAdapter,
)
from tests.control_plane.replay_harness import canonical, load_corpus


class R2CompositionReplayTests(unittest.TestCase):
    """Integration-owned gate: execute every synthetic replay against composed A-D APIs."""

    def test_all_48_replay_contracts_match_composed_production(self):
        adapter = ProductionReplayAdapter()
        failures: list[str] = []
        cases = load_corpus()
        self.assertEqual(len(cases), 48)

        for case in cases:
            try:
                actual = adapter.evaluate(case)
            except IntegrationBindingUnavailable as exc:
                failures.append(
                    f"{case.scenario_id} BINDING_UNAVAILABLE {exc}"
                )
                continue
            except Exception as exc:
                failures.append(
                    f"{case.scenario_id} EXECUTION_ERROR "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            if canonical(actual) != canonical(case.expected):
                failures.append(
                    f"{case.scenario_id} MISMATCH "
                    f"actual={canonical(actual)} "
                    f"expected={canonical(case.expected)}"
                )

        self.assertEqual(
            failures,
            [],
            "R2_COMPOSITION_REPLAY_FAILURES:\n" + "\n".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
