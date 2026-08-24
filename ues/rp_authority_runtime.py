from __future__ import annotations

import argparse
import json
import os
from typing import Any

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_observed as observed
from .current_authority import load_current_authority_json
from .observation_backed_health import (
    observation_backed_no_effect_eligible,
    run_observation_backed_no_effect_health,
)
from .rp_readonly_runtime import RP_NAMES, _load_rp_adapter


def _validated_authority(adapter: dict[str, Any]) -> dict[str, Any] | None:
    actor = str(
        os.environ.get("UES_AUTHORITY_TRANSPORT_ACTOR")
        or os.environ.get("GITHUB_ACTOR")
        or ""
    ).strip()
    return load_current_authority_json(
        adapter,
        os.environ.get("UES_CURRENT_AUTHORITY_JSON"),
        transport_actor=actor,
    )


def run(project: str) -> dict[str, Any]:
    """Run the shared current-authority lifecycle for an RP project.

    This wrapper grants no authority. Effect-capable topology still uses the shared
    live lifecycle runtime. A validated Current Authority envelope with no stable or
    dynamic lineages, no authorized initial/successor generations, and no authorized
    workflow dispatches is instead proven as a zero-effect lifecycle using the fresh
    persisted provider-observer snapshot; it performs no redundant Jules read.
    """

    project_name = str(project or "").strip().upper()
    adapter = _load_rp_adapter(project_name)
    authority = _validated_authority(adapter)

    if authority is not None and observation_backed_no_effect_eligible(adapter, authority):
        result = dict(run_observation_backed_no_effect_health(adapter, authority=authority))
    else:
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
