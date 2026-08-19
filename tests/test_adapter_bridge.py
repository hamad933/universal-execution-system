import json
import tempfile
import unittest
from pathlib import Path

from ues.bridge import execute_readonly_request, parse_exec_request


class AdapterBridgeTests(unittest.TestCase):
    def test_adapter_plan_reads_repository_contract_without_executing_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "eslint ."}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / ".ues").mkdir()
            (root / ".ues" / "project.json").write_text(
                json.dumps(
                    {
                        "adapter": {
                            "family": "web",
                            "commands": {"verify-fast": ["./tools/verify-fast"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            request = parse_exec_request("/exec adapter-plan contract=.ues/project.json")
            result = execute_readonly_request(
                request,
                root,
                repository="owner/repo",
                workstream_id="W1",
                operation_id="O1",
            )["result"]
            self.assertEqual(result["effective_capabilities"], ["node"])
            self.assertEqual(result["commands"]["lint"]["argv"], ["npm", "run", "lint"])
            self.assertEqual(result["commands"]["verify-fast"]["argv"], ["./tools/verify-fast"])
            self.assertEqual(result["execution_policy"], "plan-only-no-command-execution")

    def test_adapter_plan_rejects_contract_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = parse_exec_request("/exec adapter-plan contract=../outside.json")
            with self.assertRaisesRegex(ValueError, "escapes repository root"):
                execute_readonly_request(
                    request,
                    root,
                    repository="owner/repo",
                    workstream_id="W1",
                    operation_id="O1",
                )


if __name__ == "__main__":
    unittest.main()
