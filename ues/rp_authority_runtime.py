from __future__ import annotations

import argparse
import json
from typing import Any

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_observed as observed
from .rp_readonly_runtime import RP_NAMES, _load_rp_adapter


def run(project: str) -> dict[str, Any]:
    """Run the shared current-authority lifecycle for an RP project.

    This wrapper grants no authority. It only supplies the stable RP adapter to
    the shared runtime, which still validates the transported Drive Current
    Authority and all provider/binding/idempotency gates before any effect.
    """

    project_name = str(project or "").strip().upper()
    _load_rp_adapter(project_name)
    original_loader = legacy._load_adapter
    legacy._load_adapter = _load_rp_adapter
    try:
        result = dict(observed.run(project_name))
    finally:
        legacy._load_adapter = original_loader
    result["project"] = project_name
    result["rp_runtime_mode"] = "CURRENT_AUTHORITY_GATED"
    result["runtime_wrapper_grants_authority"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES RP current-authority lifecycle wrapper")
    parser.add_argument("project", choices=sorted(RP_NAMES))
    args = parser.parse_args(argv)
    print(json.dumps(run(args.project), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
