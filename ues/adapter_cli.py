from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import load_contract, resolve_adapter_plan
from .cli import detect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ues-adapter")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--contract")
    parser.add_argument("--registry")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    try:
        contract = load_contract(Path(args.contract)) if args.contract else None
        registry = load_contract(Path(args.registry)) if args.registry else None
        result = resolve_adapter_plan(
            repo,
            detect(repo)["capabilities"],
            contract=contract,
            registry=registry,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": "0.3", "error": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
