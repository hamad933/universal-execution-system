import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
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


class TrustedAuthorityBridgeTests(unittest.TestCase):
    def context(self, head: str, actor: str = "owner"):
        return {
            "actor": actor,
            "repository_owner": "owner",
            "event_id": "98765",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "pr_number": 9,
            "candidate_ref": "feature/x",
            "candidate_head_sha": head,
            "operation_records": [],
        }

    def test_owner_comment_builds_ready_but_non_executable_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            request = parse_exec_request(
                f"/exec mutation-authorize operation=format-fix sha={head} ref=feature/x paths=README.md resources=workspace"
            )
            result = execute_readonly_request(
                request, root, repository="owner/repo", workstream_id="W1",
                operation_id="bridge-call", default_expected_sha=head, default_ref="feature/x",
                authority_context=self.context(head),
            )["result"]
            self.assertTrue(result["transport"]["trusted"])
            self.assertEqual(result["transport"]["operation_id"], "github-comment:98765")
            self.assertEqual(result["mutation_plan"]["decision"], "AUTHORIZED_DRY_RUN")
            self.assertEqual(result["write_boundary"]["decision"], "READY_FOR_EXECUTOR_INTEGRATION")
            self.assertFalse(result["write_boundary"]["execution_enabled"])
            self.assertFalse(result["execution_enabled"])

    def test_non_owner_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            request = parse_exec_request(
                f"/exec mutation-authorize operation=format-fix sha={head} ref=feature/x paths=README.md"
            )
            with self.assertRaisesRegex(ValueError, "not repository owner"):
                execute_readonly_request(
                    request, root, repository="owner/repo", workstream_id="W1",
                    operation_id="bridge-call", default_expected_sha=head, default_ref="feature/x",
                    authority_context=self.context(head, actor="attacker"),
                )

    def test_replay_record_blocks_second_execution_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); head = init_repo(root)
            request = parse_exec_request(
                f"/exec mutation-authorize operation=format-fix sha={head} ref=feature/x paths=README.md"
            )
            first = execute_readonly_request(
                request, root, repository="owner/repo", workstream_id="W1",
                operation_id="bridge-call", default_expected_sha=head, default_ref="feature/x",
                authority_context=self.context(head),
            )["result"]
            record = dict(first["preview_receipt"], state="CONFIRMED")
            context = self.context(head); context["operation_records"] = [record]
            replay = execute_readonly_request(
                request, root, repository="owner/repo", workstream_id="W1",
                operation_id="bridge-call", default_expected_sha=head, default_ref="feature/x",
                authority_context=context,
            )["result"]
            self.assertEqual(replay["write_boundary"]["decision"], "BLOCKED")
            self.assertEqual(replay["write_boundary"]["idempotency"]["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
            self.assertFalse(replay["write_boundary"]["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
