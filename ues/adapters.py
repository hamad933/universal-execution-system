from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "adapters" / "registry.json"

LOCKFILE_PACKAGE_MANAGERS = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
)

NODE_SCRIPT_ALIASES: dict[str, tuple[str, ...]] = {
    "format-check": ("format:check", "format-check", "check:format"),
    "format-fix": ("format", "format:write", "format-fix"),
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "check:types"),
    "test-fast": ("test:fast", "test:unit", "test"),
    "test-full": ("test:full", "test"),
    "build": ("build",),
}

COMPOSER_SCRIPT_ALIASES: dict[str, tuple[str, ...]] = {
    "format-check": ("format-check", "format:check"),
    "format-fix": ("format", "format-fix"),
    "lint": ("lint",),
    "typecheck": ("typecheck", "analyse", "analyze"),
    "test-fast": ("test-fast", "test:unit", "test"),
    "test-full": ("test-full", "test"),
    "build": ("build",),
}


def load_registry(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or DEFAULT_REGISTRY_PATH
    value = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("adapter registry must be a JSON object")
    if not isinstance(value.get("capabilities"), dict):
        raise ValueError("adapter registry missing capabilities object")
    if not isinstance(value.get("families"), dict):
        raise ValueError("adapter registry missing families object")
    return value


def load_contract(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("project contract must be a JSON object")
    return value


def _node_package_manager(repo: Path) -> str | None:
    for filename, manager in LOCKFILE_PACKAGE_MANAGERS:
        if (repo / filename).exists():
            return manager
    return "npm" if (repo / "package.json").exists() else None


def _script_argv(
    scripts: dict[str, Any],
    aliases: dict[str, tuple[str, ...]],
    prefix: list[str],
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    for semantic, names in aliases.items():
        for name in names:
            if name in scripts:
                resolved[semantic] = {
                    "argv": [*prefix, name],
                    "source": "repository-script",
                    "script": name,
                }
                break
    return resolved


def _discover_commands(
    repo: Path,
    capabilities: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    commands: dict[str, dict[str, Any]] = {}
    metadata: dict[str, Any] = {}

    if "node" in capabilities and (repo / "package.json").exists():
        package = load_contract(repo / "package.json")
        scripts = package.get("scripts")
        scripts = scripts if isinstance(scripts, dict) else {}
        manager = _node_package_manager(repo)
        metadata["node"] = {"package_manager": manager}
        if manager:
            prefix = ["npm", "run"] if manager == "npm" else [manager, "run"]
            commands.update(_script_argv(scripts, NODE_SCRIPT_ALIASES, prefix))

    if "php" in capabilities and (repo / "composer.json").exists():
        composer = load_contract(repo / "composer.json")
        scripts = composer.get("scripts")
        scripts = scripts if isinstance(scripts, dict) else {}
        metadata["php"] = {"package_manager": "composer"}
        php_commands = _script_argv(scripts, COMPOSER_SCRIPT_ALIASES, ["composer", "run-script"])
        for semantic, entry in php_commands.items():
            commands.setdefault(semantic, entry)

    return commands, metadata


def _normalize_override(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) and value.strip():
        return {"shell": value.strip(), "source": "repository-override"}
    if isinstance(value, list) and value and all(isinstance(part, str) and part for part in value):
        return {"argv": value, "source": "repository-override"}
    return None


def resolve_adapter_plan(
    repo: Path,
    detected_capabilities: list[str],
    contract: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = registry or load_registry()
    adapter = contract.get("adapter", {}) if isinstance(contract, dict) else {}
    if not isinstance(adapter, dict):
        adapter = {}

    family = str(adapter.get("family") or "generic")
    families = registry["families"]
    if family not in families:
        raise ValueError(f"unknown adapter family: {family}")

    family_def = families[family]
    family_caps = family_def.get("capabilities", [])
    declared_caps = adapter.get("capabilities", [])
    disabled_caps = adapter.get("disabled_capabilities", [])

    if not isinstance(family_caps, list) or not isinstance(declared_caps, list) or not isinstance(disabled_caps, list):
        raise ValueError("adapter capabilities must be arrays")
    if not all(isinstance(item, str) for item in [*family_caps, *declared_caps, *disabled_caps]):
        raise ValueError("adapter capabilities must contain strings only")
    disabled = set(disabled_caps)

    effective = (set(detected_capabilities) | set(family_caps) | set(declared_caps)) - disabled
    known = set(registry["capabilities"])
    unknown = sorted(effective - known)
    known_effective = sorted(effective & known)

    commands, discovery_metadata = _discover_commands(repo, set(known_effective))

    override_commands = adapter.get("commands", {})
    if isinstance(override_commands, dict):
        for semantic, value in override_commands.items():
            normalized = _normalize_override(value)
            if normalized is not None:
                commands[str(semantic)] = normalized

    tools: set[str] = set()
    for capability in known_effective:
        definition = registry["capabilities"][capability]
        if not isinstance(definition, dict):
            continue
        for tool in definition.get("tools", []):
            if isinstance(tool, str):
                tools.add(tool)

    return {
        "schema_version": "0.3",
        "family": family,
        "detected_capabilities": sorted(set(detected_capabilities)),
        "effective_capabilities": known_effective,
        "unknown_capabilities": unknown,
        "disabled_capabilities": sorted(disabled),
        "required_tools": sorted(tools),
        "commands": commands,
        "metadata": discovery_metadata,
        "override_policy": "repository-overrides-win",
        "execution_policy": "plan-only-no-command-execution",
    }
