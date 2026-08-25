from __future__ import annotations

import argparse
import json
from typing import Any

from . import observation_backed_health as health
from .terminal_recovery import load_governed_projects


def _adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().upper()
    matches = [item for item in load_governed_projects([name]) if item["project"] == name]
    if len(matches) != 1:
        raise ValueError("exact governed adapter identity required")
    return dict(matches[0])


def run(project: str) -> dict[str, Any]:
    # observation_backed_health already builds the canonical live StateStore.
    # This wrapper grants no project/provider authority and adds no provider call.
    result = dict(
        health.run_observation_backed_no_effect_health(
            _adapter(project),
            authority=None,
        )
    )
    result["terminal_recovery_lifecycle"] = True
    result["runtime_wrapper_grants_authority"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES durable terminal-result lifecycle readback")
    parser.add_argument("project")
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
