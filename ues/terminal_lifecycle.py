from __future__ import annotations

import argparse
import json
from typing import Any

from . import observation_backed_health as health
from .state_backends.recovery_same_repo import build_recovery_state_store
from .terminal_recovery import load_governed_projects


def _adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().upper()
    matches = [item for item in load_governed_projects([name]) if item["project"] == name]
    if len(matches) != 1:
        raise ValueError("exact governed adapter identity required")
    return dict(matches[0])


def run(project: str) -> dict[str, Any]:
    # This CLI changes only the StateStore transport preflight used by the no-effect
    # lifecycle read. It does not grant authority or add any provider call/effect.
    original = health.build_live_state_store
    health.build_live_state_store = build_recovery_state_store
    try:
        result = dict(health.run_observation_backed_no_effect_health(_adapter(project), authority=None))
    finally:
        health.build_live_state_store = original
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
