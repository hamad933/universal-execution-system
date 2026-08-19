import subprocess
import tempfile
import unittest
from pathlib import Path

from ues.bridge import execute_readonly_request, parse_exec_request


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()


class BridgeParsingTests(unittest.TestCase):
    def test_parses_preflight(self):
        request = parse_exec_request("/exec preflight sha=abc branch=main")
        self.assertEqual(request.command, "preflight")
        self.assertEqual(request.arguments["sha"], "abc")
        self.assertEqual(request.arguments["branch"], "main")

    def test_rejects_write_command(self):
        with self.assertRaisesRegex(ValueError, "write command not enabled"):
            parse_exec_request("/exec push sha=abc")

    def test_rejects_free_form_arguments(self):
        with self.assertRaisesRegex(ValueError, "key=value"):
            parse_exec_request("/exec preflight abc")


class BridgeExecutionTests(unittest.TestCase):
    def test_preflight_uses_exact_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            request = parse_exec_request("/exec preflight")
            result = execute_readonly_request(request, root, repository="owner/repo", workstream_id="W1", operation_id="O1", default_expected_sha=head)
            self.assertTrue(result["result"]["passed"])

    def test_evidence_is_sha_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            request = parse_exec_request("/exec evidence")
            result = execute_readonly_request(request, root, repository="owner/repo", workstream_id="W1", operation_id="O1", default_expected_sha=head)["result"]
            self.assertEqual(result["start_sha"], head)
            self.assertEqual(result["final_sha"], head)
            self.assertEqual(result["changed_paths"], [])


if __name__ == "__main__":
    unittest.main()
