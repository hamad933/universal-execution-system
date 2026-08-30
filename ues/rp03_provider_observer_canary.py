from __future__ import annotations

import json
from typing import Any

from . import provider_observer
from . import provider_observer_runtime

RP03_PROJECT: dict[str, str] = {
    "project": "RP03",
    "route": "RP03",
    "repository": "hamad933/BOOKING-SERVICES",
}


def configure_rp03_scope() -> tuple[dict[str, str], ...]:
    """Bind the existing GET-only provider observer to RP03 only.

    The canary reuses the production observer implementation and changes only its
    project registry for this process. It does not authorize provider mutation.
    """
    scope = (dict(RP03_PROJECT),)
    provider_observer.PROJECTS = scope
    provider_observer_runtime.PROJECTS = scope
    return scope


def run() -> dict[str, Any]:
    configure_rp03_scope()
    result = provider_observer_runtime.observe()
    result["canary_scope"] = "RP03_ONLY"
    result["provider_mutation_authorized"] = False
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "JULES_PROVIDER_OBSERVATION_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
