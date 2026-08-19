import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ues.format_fix import PRETTIER_VERSION, format_in_sandbox, formatter_argv


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "app.js").write_text("const x={a:1}\n", encoding="utf-8")
    (root / "other.js").write_text("const y={b:2}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def prepared(head: str):
    return {
        "should_execute": True,
        "operation_id": "github-comment:1",
        "start_sha": head,
        "trusted_authority": {
            "mutation_request": {
                "operation": "format-fix",
                "proposed_paths": ["app.js"],
                "metadata": {"formatter": "prettier-pinned"},
            }
        },
    }


class FormatFixTests(unittest.TestCase):
    def test_formatter_is_pinned(self):
        argv = formatter_argv("prettier-pinned", ["app.js"])
        self.assertIn(f"prettier@{PRETTIER_VERSION}", argv)
        self.assertNotIn("latest", argv)

    def test_sandbox_generates_patch_only_for_authorized_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = init_repo(root)

            def runner(argv, cwd):
                self.assertEqual(argv[-1], "app.js")
                (cwd / "app.js").write_text("const x = { a: 1 };\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            result, patch = format_in_sandbox(prepared(head), root, runner=runner)
            self.assertEqual(result["state"], "FORMAT_READY")
            self.assertEqual(result["changed_paths"], ["app.js"])
            self.assertTrue(patch)

    def test_sandbox_rejects_formatter_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = init_repo(root)

            def runner(argv, cwd):
                (cwd / "app.js").write_text("const x = { a: 1 };\n", encoding="utf-8")
                (cwd / "other.js").write_text("const y = { b: 2 };\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaisesRegex(ValueError, "unauthorized paths"):
                format_in_sandbox(prepared(head), root, runner=runner)


if __name__ == "__main__":
    unittest.main()
