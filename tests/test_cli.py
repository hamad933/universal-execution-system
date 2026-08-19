import json
import tempfile
import unittest
from pathlib import Path

from ues.cli import detect, resource_advice, validate_contract


class DetectionTests(unittest.TestCase):
    def test_detects_only_present_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "compose.yaml").write_text("services: {}", encoding="utf-8")
            result = detect(root)
            self.assertEqual(result["capabilities"], ["docker", "node"])


class ContractTests(unittest.TestCase):
    def test_minimum_contract_is_valid(self):
        payload = {
            "schema_version": "0.1",
            "project": {"id": "X", "name": "Example"},
            "repository": {"provider": "github", "full_name": "owner/repo"},
            "adapter": {"family": "generic"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(validate_contract(path)["valid"])

    def test_missing_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text('{"schema_version":"0.1"}', encoding="utf-8")
            result = validate_contract(path)
            self.assertFalse(result["valid"])
            self.assertIn("project.id", result["missing_required_fields"])


class ResourceAdviceTests(unittest.TestCase):
    def test_prebuild_stays_off_for_infrequent_project(self):
        payload = {
            "bootstrap_seconds": 300,
            "dependency_mb": 1000,
            "uses_per_month": 2,
            "cache_hit_rate": 0.9,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = resource_advice(path)
            self.assertEqual(result["cache"]["recommendation"], "ON")
            self.assertEqual(result["prebuild"]["recommendation"], "OFF")

    def test_low_hit_cache_is_marked_for_review(self):
        payload = {
            "bootstrap_seconds": 60,
            "dependency_mb": 500,
            "uses_per_month": 10,
            "cache_hit_rate": 0.1,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = resource_advice(path)
            self.assertEqual(result["cache"]["recommendation"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
