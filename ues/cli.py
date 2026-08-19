from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


CAPABILITY_SIGNALS: dict[str, tuple[str, ...]] = {
    "node": ("package.json",),
    "php": ("composer.json",),
    "python": ("pyproject.toml", "requirements.txt", "Pipfile", "uv.lock"),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java-gradle": ("gradlew", "build.gradle", "build.gradle.kts"),
    "java-maven": ("mvnw", "pom.xml"),
    "docker": ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml"),
}

TOOL_BY_CAPABILITY = {
    "node": ("node",),
    "php": ("php", "composer"),
    "python": ("python",),
    "rust": ("cargo",),
    "go": ("go",),
    "java-gradle": ("java",),
    "java-maven": ("java",),
    "docker": ("docker",),
}

REQUIRED_CONTRACT_PATHS = (
    ("schema_version",),
    ("project", "id"),
    ("repository", "provider"),
    ("repository", "full_name"),
    ("adapter", "family"),
)


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def detect(repo: Path) -> dict[str, Any]:
    found: dict[str, list[str]] = {}
    for capability, signals in CAPABILITY_SIGNALS.items():
        matches = [signal for signal in signals if (repo / signal).exists()]
        if matches:
            found[capability] = matches
    return {
        "schema_version": "0.1",
        "repo": str(repo.resolve()),
        "capabilities": sorted(found),
        "signals": found,
    }


def status(repo: Path) -> dict[str, Any]:
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    porcelain = git(repo, "status", "--porcelain=v1")
    upstream = None
    ahead = behind = None
    try:
        upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        counts = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
        ahead, behind = int(counts[0]), int(counts[1])
    except RuntimeError:
        pass
    return {
        "schema_version": "0.1",
        "repo": str(repo.resolve()),
        "branch": branch,
        "head_sha": head,
        "tree_sha": tree,
        "dirty": bool(porcelain),
        "changed_entries": [line for line in porcelain.splitlines() if line],
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
    }


def doctor(repo: Path) -> dict[str, Any]:
    detected = detect(repo)
    required = {"git", "python"}
    for capability in detected["capabilities"]:
        required.update(TOOL_BY_CAPABILITY.get(capability, ()))
    tools = {name: shutil.which(name) for name in sorted(required)}
    missing = [name for name, location in tools.items() if location is None]
    return {
        "schema_version": "0.1",
        "ready": not missing,
        "capabilities": detected["capabilities"],
        "tools": tools,
        "missing": missing,
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    return value


def validate_contract(path: Path) -> dict[str, Any]:
    data = load_json(path)
    missing: list[str] = []
    for route in REQUIRED_CONTRACT_PATHS:
        current: Any = data
        for part in route:
            if not isinstance(current, dict) or part not in current:
                missing.append(".".join(route))
                break
            current = current[part]
    return {
        "schema_version": "0.1",
        "valid": not missing,
        "contract": str(path),
        "missing_required_fields": missing,
    }


def resource_advice(path: Path) -> dict[str, Any]:
    metrics = load_json(path)
    bootstrap_seconds = float(metrics.get("bootstrap_seconds", 0))
    dependency_mb = float(metrics.get("dependency_mb", 0))
    uses_per_month = int(metrics.get("uses_per_month", 0))
    cache_hit_rate = metrics.get("cache_hit_rate")

    cache = "OFF"
    cache_reason = "bootstrap and dependency footprint are small"
    if bootstrap_seconds >= 30 or dependency_mb >= 250:
        cache = "ON"
        cache_reason = "bootstrap cost or dependency footprint justifies caching"

    prebuild = "OFF"
    prebuild_reason = "prebuild should remain opt-in until repeated use proves value"
    if uses_per_month >= 12 and bootstrap_seconds >= 120:
        prebuild = "CONSIDER"
        prebuild_reason = "frequent use and expensive bootstrap make prebuild a candidate"

    if isinstance(cache_hit_rate, (int, float)) and cache_hit_rate < 0.2 and uses_per_month >= 4:
        cache = "REVIEW"
        cache_reason = "measured cache hit rate is low; stored cache may not justify its footprint"

    return {
        "schema_version": "0.1",
        "mode": "ADVISORY_ONLY",
        "cache": {"recommendation": cache, "reason": cache_reason},
        "prebuild": {"recommendation": prebuild, "reason": prebuild_reason},
        "inputs": {
            "bootstrap_seconds": bootstrap_seconds,
            "dependency_mb": dependency_mb,
            "uses_per_month": uses_per_month,
            "cache_hit_rate": cache_hit_rate,
        },
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ues")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("detect", "status", "doctor"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", default=".")
    contract = sub.add_parser("validate-contract")
    contract.add_argument("path")
    resources = sub.add_parser("resource-advice")
    resources.add_argument("metrics")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "detect":
            return emit(detect(Path(args.repo)))
        if args.command == "status":
            return emit(status(Path(args.repo)))
        if args.command == "doctor":
            result = doctor(Path(args.repo))
            return emit(result, 0 if result["ready"] else 2)
        if args.command == "validate-contract":
            result = validate_contract(Path(args.path))
            return emit(result, 0 if result["valid"] else 2)
        if args.command == "resource-advice":
            return emit(resource_advice(Path(args.metrics)))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return emit({"schema_version": "0.1", "error": str(exc)}, 2)
    return 2


if __name__ == "__main__":
    sys.exit(main())
