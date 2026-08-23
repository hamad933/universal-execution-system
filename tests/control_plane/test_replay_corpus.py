from __future__ import annotations

import json
import re
import unittest

from tests.control_plane.replay_harness import FIXTURE_DIR, ReferenceOracle, canonical, load_corpus


EXPECTED_IDS = {f"CP-{n:03d}" for n in range(1, 21)}
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}", re.I),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
)


class ReplayCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_corpus()
        cls.oracle = ReferenceOracle()

    def test_exact_twenty_required_scenarios_present(self):
        ids = {case.scenario_id for case in self.cases}
        self.assertEqual(ids, EXPECTED_IDS)
        self.assertEqual(len(self.cases), 20)

    def test_ids_are_unique_and_ordered(self):
        ids = [case.scenario_id for case in self.cases]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_scenario_is_synthetic_and_has_domain_owner(self):
        payload = json.loads((FIXTURE_DIR / "scenarios.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["synthetic"])
        for case in self.cases:
            self.assertTrue(case.domains)
            self.assertTrue(set(case.domains) <= {"A", "B", "C", "D", "INTEGRATION"})

    def test_reference_oracle_matches_locked_expectations(self):
        failures = []
        for case in self.cases:
            observed = self.oracle.evaluate(case)
            if canonical(observed) != canonical(case.expected):
                failures.append((case.scenario_id, observed, case.expected))
        self.assertEqual(failures, [])

    def test_fixture_serialization_is_deterministic(self):
        first = [canonical(self.oracle.evaluate(case)) for case in self.cases]
        second = [canonical(self.oracle.evaluate(case)) for case in self.cases]
        self.assertEqual(first, second)

    def test_fixtures_contain_no_obvious_secrets(self):
        for fixture in FIXTURE_DIR.glob("*.json"):
            text = fixture.read_text(encoding="utf-8")
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(text), f"secret-like value in {fixture.name}: {pattern.pattern}")

    def test_provider_failure_fixture_matches_scenario_10(self):
        matrix = json.loads((FIXTURE_DIR / "provider_failures.json").read_text(encoding="utf-8"))
        self.assertEqual([item["kind"] for item in matrix["failures"]], ["401", "403", "429", "500", "503", "network"])
        cp10 = next(case for case in self.cases if case.scenario_id == "CP-010")
        self.assertEqual(cp10.inputs["failures"], matrix["failures"])


if __name__ == "__main__":
    unittest.main()
