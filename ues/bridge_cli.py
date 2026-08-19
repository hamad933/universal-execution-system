from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bridge import execute_readonly_request, parse_exec_request


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ues-bridge")
    root.add_argument("--comment", required=True)
    root.add_argument("--repo", required=True)
    root.add_argument("--repository", required=True)
    root.add_argument("--workstream-id", required=True)
    root.add_argument("--operation-id", required=True)
    root.add_argument("--expected-sha")
    root.add_argument("--ref")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        request = parse_exec_request(args.comment)
        payload = execute_readonly_request(
            request,
            Path(args.repo),
            repository=args.repository,
            workstream_id=args.workstream_id,
            operation_id=args.operation_id,
            default_expected_sha=args.expected_sha,
            default_ref=args.ref,
        )
        print(json.dumps({"schema_version": "0.4", "ok": True, **payload}, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"schema_version": "0.4", "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
