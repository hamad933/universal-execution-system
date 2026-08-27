from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ues.providers.jules import JulesClient

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def repository_fingerprint(repository: str) -> str:
    text = str(repository or "").strip().strip("/").casefold()
    if text.count("/") != 1 or any(not part for part in text.split("/")):
        raise ValueError("repository must be owner/repo")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_candidate_hashes(raw: str | Iterable[str]) -> tuple[str, ...]:
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        digest = str(value or "").strip().casefold()
        if not digest:
            continue
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("candidate source fingerprints must be lowercase SHA-256 hex")
        if digest not in seen:
            seen.add(digest)
            normalized.append(digest)
    if not normalized:
        raise ValueError("at least one candidate source fingerprint is required")
    if len(normalized) > 32:
        raise ValueError("at most 32 candidate source fingerprints are allowed")
    return tuple(normalized)


def probe_sources(client: JulesClient, candidate_hashes: Iterable[str]) -> dict[str, Any]:
    candidates = set(parse_candidate_hashes(candidate_hashes))
    matches: set[str] = set()
    source_count = 0

    for source in client.list_sources(page_size=100):
        source_count += 1
        if not isinstance(source, Mapping):
            continue
        repository = source.get("repository")
        if not isinstance(repository, str) or not repository:
            continue
        try:
            digest = repository_fingerprint(repository)
        except ValueError:
            continue
        if digest in candidates:
            matches.add(digest)

    return {
        "schema_version": "1.0",
        "result": "JULES_SOURCE_CAPABILITY_PROBE",
        "source_inventory_read_complete": True,
        "source_count": source_count,
        "candidate_count": len(candidates),
        "match_count": len(matches),
        "matched_candidate_hashes": sorted(matches),
        "provider_mutation_performed": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "private_source_names_persisted": False,
        "source_identifiers_persisted": False,
        "safe_to_blind_retry": False,
    }


def main() -> int:
    api_key = os.environ.get("JULES_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("JULES_API_KEY is required")
    candidate_hashes = parse_candidate_hashes(os.environ.get("UES_JULES_SOURCE_PROBE_HASHES", ""))
    result = probe_sources(JulesClient(api_key), candidate_hashes)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
