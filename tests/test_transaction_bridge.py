import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ues.bridge import execute_readonly_request, parse_exec_request


def init_repo(root: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
    return head, tree


class TransactionBridgeTests(unittest.TestCase):
    def test_mutation_plan_can_authorize_only_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head, tree = init_repo(root)
            authority = {
                "schema_version": "0.4",
                "operation_id": "op-1",
                "workstream_id": "W1",
                "repository": "owner/repo",
                "ref": "feature/x",
                "expected_head_sha": head,
                "expected_tree_sha": tree,
                "operation": "format-fix",
                "allowed_paths": ["README.md"],
                "prohibited_paths": [],
                "resource_classes": [],
                "stop_gate": "HEAD_MOVED",
            }
            mutation_request = {
                "schema_version": "0.4",
                "operation": "format-fix",
                "proposed_paths": ["README.md"],
                "resource_classes": [],
            }
            (root / "authority.json").write_text(json.dumps(authority), encoding="utf-8")
            (root / "request.json").write_text(json.dumps(mutation_request), encoding="utf-8")

            request = parse_exec_request(
                "/exec mutation-plan authority=authority.json request=request.json"
            )
            result = execute_readonly_request(
                request,
                root,
                repository="owner/repo",
                workstream_id="W1",
                operation_id="bridge-invocation-1",
                default_expected_sha=head,
                default_ref="feature/x",
            )["result"]
            self.assertEqual(result["decision"], "AUTHORIZED_DRY_RUN")
            self.assertFalse(result["execution_enabled"])
            self.assertFalse(result["safe_to_execute_now"])

    def test_mutation_plan_rejects_moved_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head, tree = init_repo(root)
            authority = {
                "schema_version": "0.4",
                "operation_id": "op-1",
                "workstream_id": "W1",
                "repository": "owner/repo",
                "ref": "feature/x",
                "expected_head_sha": "0" * 40,
                "expected_tree_sha": tree,
                "operation": "format-fix",
                "allowed_paths": ["README.md"],
                "stop_gate": "HEAD_MOVED",
            }
            mutation_request = {
                "schema_version": "0.4",
                "operation": "format-fix",
                "proposed_paths": ["README.md"],
            }
            (root / "authority.json").write_text(json.dumps(authority), encoding="utf-8")
            (root / "request.json").write_text(json.dumps(mutation_request), encoding="utf-8")
            request = parse_exec_request(
                "/exec mutation-plan authority=authority.json request=request.json"
            )
            result = execute_readonly_request(
                request,
                root,
                repository="owner/repo",
                workstream_id="W1",
                operation_id="bridge-invocation-2",
                default_expected_sha=head,
                default_ref="feature/x",
            )["result"]
            self.assertEqual(result["decision"], "REJECTED")
            self.assertIn("HEAD_MISMATCH", [item["code"] for item in result["authority"]["failures"]])

    def test_mutation_plan_rejects_path_escape_for_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            request = parse_exec_request(
                "/exec mutation-plan authority=../authority.json request=request.json"
            )
            with self.assertRaisesRegex(ValueError, "path escapes repository root"):
                execute_readonly_request(
                    request,
                    root,
                    repository="owner/repo",
                    workstream_id="W1",
                    operation_id="bridge-invocation-3",
                    default_ref="feature/x",
                )


if __name__ == "__main__":
    unittest.main()
