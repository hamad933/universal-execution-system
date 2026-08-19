import json
import tempfile
import unittest
from pathlib import Path

from ues.adapters import resolve_adapter_plan


REGISTRY = {
    "schema_version": "0.3",
    "families": {
        "generic": {"capabilities": []},
        "web": {"capabilities": []},
        "python": {"capabilities": ["python"]},
    },
    "capabilities": {
        "node": {"tools": ["node"]},
        "php": {"tools": ["php", "composer"]},
        "python": {"tools": ["python"]},
        "docker": {"tools": ["docker"]},
    },
}


class AdapterPlanTests(unittest.TestCase):
    def test_composes_detected_capabilities_without_bloat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "eslint .", "build": "vite build"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            result = resolve_adapter_plan(root, ["node"], registry=REGISTRY)
            self.assertEqual(result["effective_capabilities"], ["node"])
            self.assertEqual(result["required_tools"], ["node"])
            self.assertEqual(result["commands"]["lint"]["argv"], ["npm", "run", "lint"])
            self.assertNotIn("php", result["effective_capabilities"])

    def test_repository_argv_override_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest"}}),
                encoding="utf-8",
            )
            contract = {
                "adapter": {
                    "family": "web",
                    "capabilities": ["node"],
                    "commands": {"test-fast": ["./tools/verify-fast"]},
                }
            }
            result = resolve_adapter_plan(root, ["node"], contract=contract, registry=REGISTRY)
            self.assertEqual(result["commands"]["test-fast"]["argv"], ["./tools/verify-fast"])
            self.assertEqual(result["commands"]["test-fast"]["source"], "repository-override")

    def test_compatibility_family_adds_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_adapter_plan(
                Path(directory),
                [],
                contract={"adapter": {"family": "python"}},
                registry=REGISTRY,
            )
            self.assertEqual(result["effective_capabilities"], ["python"])

    def test_disabled_capability_removes_detected_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = {
                "adapter": {
                    "family": "generic",
                    "disabled_capabilities": ["docker"],
                }
            }
            result = resolve_adapter_plan(root, ["docker", "python"], contract=contract, registry=REGISTRY)
            self.assertEqual(result["effective_capabilities"], ["python"])

    def test_unknown_capability_is_reported_but_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_adapter_plan(
                Path(directory),
                [],
                contract={"adapter": {"family": "generic", "capabilities": ["quantum-runtime"]}},
                registry=REGISTRY,
            )
            self.assertEqual(result["effective_capabilities"], [])
            self.assertEqual(result["unknown_capabilities"], ["quantum-runtime"])

    def test_unknown_family_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unknown adapter family"):
                resolve_adapter_plan(
                    Path(directory),
                    [],
                    contract={"adapter": {"family": "mystery"}},
                    registry=REGISTRY,
                )


if __name__ == "__main__":
    unittest.main()
