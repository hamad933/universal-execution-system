import json
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


class ControlBridgeTests(unittest.TestCase):
    def test_failure_classification_routes_shared_baseline_out_of_workstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "failure.json").write_text(
                json.dumps({"origin": "candidate", "stage": "test", "base_reproduces": True}),
                encoding="utf-8",
            )
            request = parse_exec_request("/exec failure-classify input=failure.json")
            result = execute_readonly_request(
                request,
                root,
                repository="owner/repo",
                workstream_id="W1",
                operation_id="O1",
            )["result"]
            self.assertEqual(result["classification"]["category"], "SHARED_BASELINE_DEFECT")
            self.assertEqual(result["blocker_scope"]["blocks"], [])

    def test_reconcile_detects_matching_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = init_repo(root)
            (root / "checkpoint.json").write_text(
                json.dumps({"confirmed_head_sha": head, "write_outcome": "CONFIRMED"}),
                encoding="utf-8",
            )
            request = parse_exec_request("/exec reconcile checkpoint=checkpoint.json")
            result = execute_readonly_request(
                request,
                root,
                repository="owner/repo",
                workstream_id="W1",
                operation_id="O1",
            )["result"]
            self.assertEqual(result["verdict"], "CHECKPOINT_MATCH")
            self.assertFalse(result["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
