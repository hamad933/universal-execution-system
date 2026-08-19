from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli import detect, doctor, evidence, preflight, status


READ_ONLY_COMMANDS = {
    "capabilities",
    "detect",
    "doctor",
    "evidence",
    "preflight",
    "status",
}

WRITE_COMMANDS = {
    "apply-change-set",
    "commit",
    "create-branch",
    "create-pr",
    "format-fix",
    "push",
}


@dataclass(frozen=True)
class ExecRequest:
    command: str
    arguments: dict[str, str]


def parse_exec_request(comment: str) -> ExecRequest:
    first_line = next((line.strip() for line in comment.splitlines() if line.strip()), "")
    if not first_line.startswith("/exec"):
        raise ValueError("comment does not start with /exec")
    parts = shlex.split(first_line)
    if len(parts) < 2:
        raise ValueError("missing /exec command")
    command = parts[1]
    if command in WRITE_COMMANDS:
        raise ValueError(f"write command not enabled in read-only bridge: {command}")
    if command not in READ_ONLY_COMMANDS:
        raise ValueError(f"unsupported read-only command: {command}")
    arguments: dict[str, str] = {}
    for token in parts[2:]:
        if "=" not in token:
            raise ValueError(f"arguments must use key=value syntax: {token}")
        key, value = token.split("=", 1)
        if not key or key in arguments:
            raise ValueError(f"invalid or duplicate argument: {key}")
        arguments[key.replace("-", "_")] = value
    return ExecRequest(command=command, arguments=arguments)


def _only(arguments: dict[str, str], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unsupported arguments: {', '.join(unknown)}")


def execute_readonly_request(
    request: ExecRequest,
    repo: Path,
    *,
    repository: str,
    workstream_id: str,
    operation_id: str,
    default_expected_sha: str | None = None,
) -> dict[str, Any]:
    args = request.arguments
    command = request.command

    if command in {"capabilities", "detect"}:
        _only(args, set())
        return {"bridge_command": command, "result": detect(repo)}

    if command == "status":
        _only(args, set())
        return {"bridge_command": command, "result": status(repo)}

    if command == "doctor":
        _only(args, set())
        return {"bridge_command": command, "result": doctor(repo)}

    if command == "preflight":
        _only(args, {"sha", "branch", "allow_dirty"})
        expected_sha = args.get("sha") or default_expected_sha
        if not expected_sha:
            raise ValueError("preflight requires sha=<expected SHA>")
        allow_dirty = args.get("allow_dirty", "false").lower() in {"1", "true", "yes"}
        return {
            "bridge_command": command,
            "result": preflight(
                repo,
                expected_sha=expected_sha,
                expected_branch=args.get("branch"),
                require_clean=not allow_dirty,
            ),
        }

    if command == "evidence":
        _only(args, {"start_sha", "base_sha"})
        start_sha = args.get("start_sha") or default_expected_sha
        if not start_sha:
            raise ValueError("evidence requires start_sha=<SHA>")
        return {
            "bridge_command": command,
            "result": evidence(
                repo,
                repository=repository,
                workstream_id=workstream_id,
                operation_id=operation_id,
                start_sha=start_sha,
                base_sha=args.get("base_sha"),
            ),
        }

    raise ValueError(f"unsupported read-only command: {command}")
