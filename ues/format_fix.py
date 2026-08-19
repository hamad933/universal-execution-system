from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

PRETTIER_VERSION = "3.9.6"
TRUSTED_FORMATTERS = {"prettier-pinned"}


def _run(argv: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=check, text=True, capture_output=True)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = _run(["git", *args], cwd=repo, check=check)
    return result.stdout.strip()


def validate_exact_paths(repo: Path, paths: list[str]) -> list[str]:
    root = repo.resolve()
    normalized: list[str] = []
    for raw in paths:
        if not raw or raw.startswith("/") or "\\" in raw:
            raise ValueError(f"unsafe format path: {raw}")
        candidate = root / raw
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"format path escapes repository: {raw}") from exc
        if candidate.is_symlink():
            raise ValueError(f"format-fix does not operate on symlinks: {raw}")
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"format-fix path must be an existing file: {raw}")
        normalized.append(candidate.relative_to(root).as_posix())
    if len(set(normalized)) != len(normalized):
        raise ValueError("format-fix paths must be unique")
    return normalized


def formatter_argv(formatter: str, paths: list[str]) -> list[str]:
    if formatter != "prettier-pinned":
        raise ValueError(f"unsupported trusted formatter: {formatter}")
    return ["npx", "--yes", f"prettier@{PRETTIER_VERSION}", "--write", "--", *paths]


def _status_entries(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="strict")
    if not raw:
        return []
    return [entry for entry in raw.split("\0") if entry]


def _changed_paths(repo: Path) -> list[str]:
    output = _git(repo, "diff", "--name-only", "--no-ext-diff")
    return [line for line in output.splitlines() if line]


def format_in_sandbox(
    prepared: dict[str, Any],
    repo: Path,
    *,
    runner: Callable[[list[str], Path], Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    if not prepared.get("should_execute"):
        raise ValueError("prepared operation is not executable")
    authority = prepared.get("trusted_authority") or {}
    request = authority.get("mutation_request") or {}
    if request.get("operation") != "format-fix":
        raise ValueError("sandbox formatter only supports format-fix")

    start_sha = str(prepared.get("start_sha") or "")
    live_sha = _git(repo, "rev-parse", "HEAD")
    if live_sha != start_sha:
        raise ValueError("sandbox checkout HEAD does not match authorized start SHA")

    if _status_entries(repo):
        raise ValueError("sandbox worktree must start clean")

    paths = validate_exact_paths(repo, [str(item) for item in request.get("proposed_paths", [])])
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    formatter = str(metadata.get("formatter") or "prettier-pinned")
    argv = formatter_argv(formatter, paths)

    env = os.environ.copy()
    env.setdefault("npm_config_audit", "false")
    env.setdefault("npm_config_fund", "false")
    if runner is None:
        completed = subprocess.run(argv, cwd=repo, env=env, text=True, capture_output=True)
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    else:
        result = runner(argv, repo)
        stdout = str(getattr(result, "stdout", ""))
        stderr = str(getattr(result, "stderr", ""))
        returncode = int(getattr(result, "returncode", 0))

    if returncode != 0:
        return ({
            "schema_version": "0.6",
            "state": "FORMAT_FAILED",
            "operation_id": prepared.get("operation_id"),
            "start_sha": start_sha,
            "formatter": formatter,
            "formatter_version": PRETTIER_VERSION,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "safe_to_blind_retry": False,
        }, b"")

    status_entries = _status_entries(repo)
    if any(entry.startswith("?? ") for entry in status_entries):
        raise ValueError("formatter created untracked files; format-fix rejects generated files")

    changed = _changed_paths(repo)
    unauthorized = sorted(set(changed) - set(paths))
    if unauthorized:
        raise ValueError(f"formatter changed unauthorized paths: {', '.join(unauthorized)}")

    patch = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "--no-color"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    patch_sha256 = hashlib.sha256(patch).hexdigest()
    state = "NO_CHANGE" if not changed else "FORMAT_READY"
    return ({
        "schema_version": "0.6",
        "state": state,
        "operation_id": prepared.get("operation_id"),
        "start_sha": start_sha,
        "formatter": formatter,
        "formatter_version": PRETTIER_VERSION,
        "authorized_paths": paths,
        "changed_paths": changed,
        "patch_sha256": patch_sha256,
        "safe_to_blind_retry": False,
    }, patch)
