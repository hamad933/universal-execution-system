from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .format_fix import format_in_sandbox
from .operation_records import render_receipt_comment
from .write_executor import apply_format_patch, fallback_final_receipt, prepare_format_fix
from .write_recovery import recover_unobserved_format_operations


def _load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ues-write")
    commands = root.add_subparsers(dest="command", required=True)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("--comment", required=True)
    authorize.add_argument("--actor", required=True)
    authorize.add_argument("--repository-owner", required=True)
    authorize.add_argument("--repository", required=True)
    authorize.add_argument("--pr-number", required=True, type=int)
    authorize.add_argument("--comment-id", required=True)
    authorize.add_argument("--comment-created-at", required=True)
    authorize.add_argument("--candidate-ref", required=True)
    authorize.add_argument("--candidate-sha", required=True)
    authorize.add_argument("--candidate-tree", required=True)
    authorize.add_argument("--workstream-id", required=True)
    authorize.add_argument("--comments-json", required=True)
    authorize.add_argument("--prepared-output", required=True)
    authorize.add_argument("--receipt-output", required=True)

    format_cmd = commands.add_parser("format")
    format_cmd.add_argument("--prepared", required=True)
    format_cmd.add_argument("--repo", required=True)
    format_cmd.add_argument("--result-output", required=True)
    format_cmd.add_argument("--patch-output", required=True)

    apply_cmd = commands.add_parser("apply")
    apply_cmd.add_argument("--prepared", required=True)
    apply_cmd.add_argument("--format-result", required=True)
    apply_cmd.add_argument("--patch", required=True)
    apply_cmd.add_argument("--repo", required=True)
    apply_cmd.add_argument("--receipt-output", required=True)

    render = commands.add_parser("render")
    render.add_argument("--receipt", required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--prepared", required=True)
    finalize.add_argument("--format-result")
    finalize.add_argument("--final-receipt")
    finalize.add_argument("--receipt-output", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "authorize":
            comments = _load(args.comments_json)
            if not isinstance(comments, list):
                raise ValueError("comments JSON must be an array")

            recoveries = recover_unobserved_format_operations(
                comments,
                repository=args.repository,
                ref=args.candidate_ref,
                live_head_sha=args.candidate_sha,
            )
            effective_comments = list(comments)
            for receipt in recoveries:
                effective_comments.append(
                    {"author": "github-actions[bot]", "body": render_receipt_comment(receipt)}
                )

            prepared = prepare_format_fix(
                args.comment,
                actor=args.actor,
                repository_owner=args.repository_owner,
                repository=args.repository,
                pr_number=args.pr_number,
                comment_id=args.comment_id,
                comment_created_at=args.comment_created_at,
                candidate_ref=args.candidate_ref,
                candidate_head_sha=args.candidate_sha,
                candidate_tree_sha=args.candidate_tree,
                workstream_id=args.workstream_id,
                prior_comments=effective_comments,
            )
            prepared["recovery_receipts"] = recoveries
            _write(args.prepared_output, prepared)
            receipt = prepared.get("receipt")
            if prepared.get("publish_receipt") and isinstance(receipt, dict):
                _write(args.receipt_output, receipt)
            print(json.dumps(prepared, indent=2, sort_keys=True))
            return 0

        if args.command == "format":
            prepared = _load(args.prepared)
            result, patch = format_in_sandbox(prepared, Path(args.repo))
            _write(args.result_output, result)
            Path(args.patch_output).write_bytes(patch)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("state") in {"FORMAT_READY", "NO_CHANGE"} else 2

        if args.command == "apply":
            prepared = _load(args.prepared)
            format_result = _load(args.format_result)
            patch = Path(args.patch).read_bytes()
            receipt = apply_format_patch(prepared, format_result, patch, Path(args.repo))
            _write(args.receipt_output, receipt)
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0 if receipt.get("state") == "CONFIRMED" else 2

        if args.command == "render":
            receipt = _load(args.receipt)
            print(render_receipt_comment(receipt))
            return 0

        if args.command == "finalize":
            prepared = _load(args.prepared)
            final_path = Path(args.final_receipt) if args.final_receipt else None
            if final_path and final_path.exists():
                receipt = _load(final_path)
            else:
                format_result = None
                if args.format_result and Path(args.format_result).exists():
                    format_result = _load(args.format_result)
                    if format_result.get("state") == "FORMAT_CRASH":
                        format_result = {
                            **format_result,
                            "state": "FORMAT_FAILED",
                            "stderr": "formatter sandbox terminated before producing a structured result",
                        }
                receipt = fallback_final_receipt(prepared, format_result=format_result)
            _write(args.receipt_output, receipt)
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0

        raise ValueError(f"unsupported write command: {args.command}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": "0.6", "ok": False, "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
