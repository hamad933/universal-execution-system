import hashlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ues.operation_records import render_receipt_comment
from ues.write_executor import apply_format_patch, prepare_format_fix


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def init_remote_repo(root: Path) -> tuple[Path, Path, str, str]:
    remote = root / "remote.git"
    work = root / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    work.mkdir()
    subprocess.run(["git", "init", "-b", "feature/x"], cwd=work, check=True, capture_output=True)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    (work / "app.js").write_text("const x={a:1}\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "feature/x")
    head = git(work, "rev-parse", "HEAD")
    tree = git(work, "rev-parse", "HEAD^{tree}")
    return remote, work, head, tree


def prepare(head: str, tree: str, comments=None, comment_id="100"):
    return prepare_format_fix(
        f"/exec format-fix sha={head} ref=feature/x paths=app.js formatter=prettier-pinned",
        actor="owner",
        repository_owner="owner",
        repository="owner/repo",
        pr_number=7,
        comment_id=comment_id,
        comment_created_at=datetime.now(timezone.utc).isoformat(),
        candidate_ref="feature/x",
        candidate_head_sha=head,
        candidate_tree_sha=tree,
        workstream_id="PR-7",
        prior_comments=comments or [],
    )


class WriteExecutorTests(unittest.TestCase):
    def test_new_owner_operation_is_planned(self):
        prepared = prepare("a" * 40, "b" * 40)
        self.assertTrue(prepared["should_execute"])
        self.assertEqual(prepared["receipt"]["state"], "PLANNED")
        self.assertEqual(prepared["operation_id"], "github-comment:100")

    def test_confirmed_receipt_blocks_replay(self):
        first = prepare("a" * 40, "b" * 40)
        receipt = dict(first["receipt"])
        receipt["state"] = "CONFIRMED"
        body = render_receipt_comment(receipt)
        replay = prepare(
            "a" * 40,
            "b" * 40,
            comments=[{"author": "github-actions[bot]", "body": body}],
        )
        self.assertFalse(replay["should_execute"])
        self.assertEqual(replay["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
        self.assertFalse(replay["publish_receipt"])

    def test_apply_patch_pushes_only_authorized_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, work, head, tree = init_remote_repo(root)
            prepared = prepare(head, tree)

            (work / "app.js").write_text("const x = { a: 1 };\n", encoding="utf-8")
            patch = subprocess.run(
                ["git", "diff", "--binary", "--no-ext-diff", "--no-color"],
                cwd=work,
                check=True,
                capture_output=True,
            ).stdout
            subprocess.run(["git", "reset", "--hard", head], cwd=work, check=True, capture_output=True)
            format_result = {
                "schema_version": "0.6",
                "state": "FORMAT_READY",
                "operation_id": prepared["operation_id"],
                "start_sha": head,
                "formatter": "prettier-pinned",
                "formatter_version": "3.9.6",
                "changed_paths": ["app.js"],
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
            }
            receipt = apply_format_patch(prepared, format_result, patch, work)
            self.assertEqual(receipt["state"], "CONFIRMED")
            self.assertNotEqual(receipt["final_sha"], head)
            remote_head = subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/feature/x"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(remote_head, receipt["final_sha"])

    def test_apply_rejects_moved_remote_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, work, head, tree = init_remote_repo(root)
            prepared = prepare(head, tree)

            other = root / "other"
            subprocess.run(["git", "clone", str(root / "remote.git"), str(other)], check=True, capture_output=True)
            subprocess.run(["git", "checkout", "feature/x"], cwd=other, check=True, capture_output=True)
            git(other, "config", "user.email", "other@example.com")
            git(other, "config", "user.name", "Other")
            (other / "other.txt").write_text("move\n", encoding="utf-8")
            git(other, "add", ".")
            git(other, "commit", "-m", "move")
            git(other, "push", "origin", "feature/x")

            format_result = {
                "state": "NO_CHANGE",
                "operation_id": prepared["operation_id"],
                "start_sha": head,
                "changed_paths": [],
                "patch_sha256": hashlib.sha256(b"").hexdigest(),
            }
            receipt = apply_format_patch(prepared, format_result, b"", work)
            self.assertEqual(receipt["state"], "REJECTED")
            self.assertEqual(
                receipt["extensions"]["failure"], "REMOTE_HEAD_MOVED_BEFORE_APPLY"
            )


if __name__ == "__main__":
    unittest.main()
