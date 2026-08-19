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
    root.add_argument("--authority-actor")
    root.add_argument("--authority-owner")
    root.add_argument("--authority-event-id")
    root.add_argument("--authority-issued-at")
    root.add_argument("--pr-number", type=int)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        request = parse_exec_request(args.comment)
        authority_context = None
        if any((args.authority_actor, args.authority_owner, args.authority_event_id, args.authority_issued_at, args.pr_number)):
            authority_context = {
                "actor": args.authority_actor,
                "repository_owner": args.authority_owner,
                "event_id": args.authority_event_id,
                "issued_at": args.authority_issued_at,
                "pr_number": args.pr_number,
                "candidate_ref": args.ref,
                "candidate_head_sha": args.expected_sha,
                "operation_records": [],
            }
        payload = execute_readonly_request(
            request,
            Path(args.repo),
            repository=args.repository,
            workstream_id=args.workstream_id,
            operation_id=args.operation_id,
            default_expected_sha=args.expected_sha,
            default_ref=args.ref,
            authority_context=authority_context,
        )
        print(json.dumps({"schema_version": "0.5", "ok": True, **payload}, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"schema_version": "0.5", "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
