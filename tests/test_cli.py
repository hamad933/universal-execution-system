import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ues.cli import detect, evidence, preflight, resource_advice, validate_contract


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()


class DetectionTests(unittest.TestCase):
    def test_detects_only_present_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "compose.yaml").write_text("services: {}", encoding="utf-8")
            self.assertEqual(detect(root)["capabilities"], ["docker", "node"])


class PreflightTests(unittest.TestCase):
    def test_exact_clean_head_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            self.assertTrue(preflight(root, head, expected_branch="main")["passed"])

    def test_wrong_head_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            result = preflight(root, "0" * 40)
            self.assertFalse(result["passed"])
            self.assertEqual(result["failures"][0]["code"], "HEAD_MISMATCH")

    def test_dirty_worktree_fails_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            (root / "README.md").write_text("changed\n", encoding="utf-8")
            codes = [failure["code"] for failure in preflight(root, head)["failures"]]
            self.assertIn("DIRTY_WORKTREE", codes)


class EvidenceTests(unittest.TestCase):
    def test_evidence_binds_start_and_final_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            result = evidence(root, "owner/repo", "W1", "O1", head)
            self.assertEqual(result["start_sha"], head)
            self.assertEqual(result["final_sha"], head)
            self.assertEqual(result["changed_paths"], [])
            self.assertEqual(result["state"], "PREFLIGHTED")


class ContractTests(unittest.TestCase):
    def test_minimum_contract_is_valid(self):
        payload = {"schema_version": "0.1", "project": {"id": "X", "name": "Example"}, "repository": {"provider": "github", "full_name": "owner/repo"}, "adapter": {"family": "generic"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(validate_contract(path)["valid"])

    def test_missing_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"; path.write_text('{"schema_version":"0.1"}', encoding="utf-8")
            result = validate_contract(path)
            self.assertFalse(result["valid"])
            self.assertIn("project.id", result["missing_required_fields"])


class ResourceAdviceTests(unittest.TestCase):
    def test_prebuild_stays_off_for_infrequent_project(self):
        payload = {"bootstrap_seconds": 300, "dependency_mb": 1000, "uses_per_month": 2, "cache_hit_rate": 0.9}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            result = resource_advice(path)
            self.assertEqual(result["cache"]["recommendation"], "ON")
            self.assertEqual(result["prebuild"]["recommendation"], "OFF")

    def test_low_hit_cache_is_marked_for_review(self):
        payload = {"bootstrap_seconds": 60, "dependency_mb": 500, "uses_per_month": 10, "cache_hit_rate": 0.1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(resource_advice(path)["cache"]["recommendation"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
