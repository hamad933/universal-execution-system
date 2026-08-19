from __future__ import annotations

import argparse
import json
import platform
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
        "schema_version": "0.2",
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
        "schema_version": "0.2",
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


def preflight(
    repo: Path,
    expected_sha: str,
    expected_branch: str | None = None,
    require_clean: bool = True,
) -> dict[str, Any]:
    snapshot = status(repo)
    failures: list[dict[str, Any]] = []
    if snapshot["head_sha"] != expected_sha:
        failures.append({"code": "HEAD_MISMATCH", "expected": expected_sha, "actual": snapshot["head_sha"]})
    if expected_branch is not None and snapshot["branch"] != expected_branch:
        failures.append({"code": "BRANCH_MISMATCH", "expected": expected_branch, "actual": snapshot["branch"]})
    if require_clean and snapshot["dirty"]:
        failures.append({"code": "DIRTY_WORKTREE", "entries": snapshot["changed_entries"]})
    return {
        "schema_version": "0.2",
        "operation": "preflight",
        "passed": not failures,
        "expected_sha": expected_sha,
        "expected_branch": expected_branch,
        "require_clean": require_clean,
        "snapshot": snapshot,
        "failures": failures,
    }


def doctor(repo: Path) -> dict[str, Any]:
    detected = detect(repo)
    required = {"git", "python"}
    for capability in detected["capabilities"]:
        required.update(TOOL_BY_CAPABILITY.get(capability, ()))
    tools = {name: shutil.which(name) for name in sorted(required)}
    missing = [name for name, location in tools.items() if location is None]
    return {
        "schema_version": "0.2",
        "ready": not missing,
        "capabilities": detected["capabilities"],
        "tools": tools,
        "missing": missing,
    }


def environment_fingerprint(repo: Path) -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "capabilities": detect(repo)["capabilities"],
    }


def changed_paths(repo: Path, start_sha: str, end_sha: str = "HEAD") -> list[str]:
    output = git(repo, "diff", "--name-only", f"{start_sha}..{end_sha}")
    return [line for line in output.splitlines() if line]


def evidence(
    repo: Path,
    repository: str,
    workstream_id: str,
    operation_id: str,
    start_sha: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    snapshot = status(repo)
    start_tree = git(repo, "rev-parse", f"{start_sha}^{{tree}}")
    return {
        "schema_version": "0.2",
        "repository": repository,
        "workstream_id": workstream_id,
        "operation_id": operation_id,
        "branch": snapshot["branch"],
        "base_sha": base_sha,
        "start_sha": start_sha,
        "start_tree_sha": start_tree,
        "final_sha": snapshot["head_sha"],
        "final_tree_sha": snapshot["tree_sha"],
        "rollback_sha": start_sha,
        "changed_paths": changed_paths(repo, start_sha),
        "commands": [],
        "checks": [],
        "ci_runs": [],
        "artifacts": [],
        "failure_classifications": [],
        "limitations": [],
        "state": "PREFLIGHTED",
        "environment_fingerprint": environment_fingerprint(repo),
        "extensions": {"dirty": snapshot["dirty"], "changed_entries": snapshot["changed_entries"]},
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
    return {"schema_version": "0.2", "valid": not missing, "contract": str(path), "missing_required_fields": missing}


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
        "schema_version": "0.2",
        "mode": "ADVISORY_ONLY",
        "cache": {"recommendation": cache, "reason": cache_reason},
        "prebuild": {"recommendation": prebuild, "reason": prebuild_reason},
        "inputs": {"bootstrap_seconds": bootstrap_seconds, "dependency_mb": dependency_mb, "uses_per_month": uses_per_month, "cache_hit_rate": cache_hit_rate},
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ues")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("detect", "status", "doctor"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", default=".")
    pre = sub.add_parser("preflight")
    pre.add_argument("--repo", default=".")
    pre.add_argument("--expected-sha", required=True)
    pre.add_argument("--expected-branch")
    pre.add_argument("--allow-dirty", action="store_true")
    ev = sub.add_parser("evidence")
    ev.add_argument("--repo", default=".")
    ev.add_argument("--repository", required=True)
    ev.add_argument("--workstream-id", required=True)
    ev.add_argument("--operation-id", required=True)
    ev.add_argument("--start-sha", required=True)
    ev.add_argument("--base-sha")
    contract = sub.add_parser("validate-contract")
    contract.add_argument("path")
    resources = sub.add_parser("resource-advice")
    resources.add_argument("metrics")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "detect": return emit(detect(Path(args.repo)))
        if args.command == "status": return emit(status(Path(args.repo)))
        if args.command == "doctor":
            result = doctor(Path(args.repo)); return emit(result, 0 if result["ready"] else 2)
        if args.command == "preflight":
            result = preflight(Path(args.repo), args.expected_sha, args.expected_branch, not args.allow_dirty)
            return emit(result, 0 if result["passed"] else 3)
        if args.command == "evidence":
            return emit(evidence(Path(args.repo), args.repository, args.workstream_id, args.operation_id, args.start_sha, args.base_sha))
        if args.command == "validate-contract":
            result = validate_contract(Path(args.path)); return emit(result, 0 if result["valid"] else 2)
        if args.command == "resource-advice": return emit(resource_advice(Path(args.metrics)))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return emit({"schema_version": "0.2", "error": str(exc)}, 2)
    return 2


if __name__ == "__main__":
    sys.exit(main())
