from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import lifecycle_runtime as legacy
from .generation_transition import initial_lineage_transition_key
from .initial_lineage_effects import execute_initial_lineage_generation
from .initial_lineage_reconciliation import reconcile_unknown_initial_lineage
from .jules_source_probe import repository_fingerprint
from .lineage_registry import lineage_lane_id
from .policy_resolution import resolve_execution_policy
from .providers.base import NetworkError, ProviderError, RateLimitError, ServerError
from .structured_handoff import build_required_handoff_instructions

SCHEMA_VERSION = "1.0"
SUPPLEMENT_POLICY_KEY = "evidence_supplement_lineages"
_ALLOWED_ROLE = "ASSURANCE"
_MAX_ATTESTATION_AGE_SECONDS = 30 * 60
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
_ALLOWED_LANE_FIELDS = frozenset(
    {
        "authorized",
        "creation_kind",
        "task_spec",
        "target_ref",
        "target_candidate_sha",
        "transport_repository_fingerprint",
        "transport_starting_branch",
        "transport_head_sha",
        "transport_attested_at",
        "evidence_root",
        "governed_packet_sha256",
        "decoded_evidence_sha256",
    }
)
_ALLOWED_TASK_FIELDS = frozenset(
    {
        "objective",
        "exact_baseline",
        "write_scope",
        "writeScope",
        "prohibited_scope",
        "prohibitedScope",
        "validation",
        "tests",
        "evidence",
        "handoff",
        "stop_gate",
        "stopGate",
    }
)
_PROVIDER_READ_ERRORS = (NetworkError, RateLimitError, ServerError)


def evidence_supplement_entries(authority: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(authority, Mapping):
        return {}
    policy = authority.get("generation_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    entries = policy.get(SUPPLEMENT_POLICY_KEY)
    return entries if isinstance(entries, Mapping) else {}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _list_field(task_spec: Mapping[str, Any], *keys: str, nonempty: bool = False) -> list[str]:
    present = [key for key in keys if key in task_spec]
    if len(present) != 1:
        raise ValueError(f"task_spec.{keys[0]} must use exactly one supported alias")
    value = task_spec.get(present[0])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"task_spec.{present[0]} must contain only non-empty strings")
    result = [item.strip() for item in value]
    if nonempty and not result:
        raise ValueError(f"task_spec.{present[0]} must not be empty")
    return result


def _text_field(task_spec: Mapping[str, Any], *keys: str) -> str:
    present = [key for key in keys if key in task_spec]
    if len(present) != 1:
        raise ValueError(f"task_spec.{keys[0]} must use exactly one supported alias")
    return _required_text(task_spec.get(present[0]), f"task_spec.{present[0]}")


def _validate_task_spec(task_spec: Mapping[str, Any], *, target_ref: str, candidate_sha: str) -> dict[str, Any]:
    unknown = sorted(str(key) for key in task_spec if key not in _ALLOWED_TASK_FIELDS)
    if unknown:
        raise ValueError("task_spec contains unsupported fields: " + ", ".join(unknown))
    result = dict(task_spec)
    _text_field(task_spec, "objective")
    exact = _text_field(task_spec, "exact_baseline")
    write_scope = _list_field(task_spec, "write_scope", "writeScope")
    _list_field(task_spec, "prohibited_scope", "prohibitedScope")
    _list_field(task_spec, "validation", "tests", nonempty=True)
    _list_field(task_spec, "evidence", nonempty=True)
    _text_field(task_spec, "handoff")
    _text_field(task_spec, "stop_gate", "stopGate")
    if write_scope:
        raise ValueError("evidence supplement ASSURANCE task must have write_scope=[]")
    if exact != f"{target_ref}@{candidate_sha}":
        raise ValueError("task_spec.exact_baseline must bind the exact governed product candidate")
    return result


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("transport_attested_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("transport_attested_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_lane(key: str, raw: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    workstream, sep, role = str(key or "").rpartition(":")
    role = role.upper()
    if not sep or not _WORKSTREAM.fullmatch(workstream) or role != _ALLOWED_ROLE:
        raise ValueError("supplement authority key must be <workstream>:ASSURANCE")
    unknown = sorted(str(field) for field in raw if field not in _ALLOWED_LANE_FIELDS)
    if unknown:
        raise ValueError("supplement authority contains unsupported fields: " + ", ".join(unknown))
    if raw.get("authorized") is not True:
        raise ValueError("supplement authority must be explicitly authorized")
    if str(raw.get("creation_kind") or "").upper() != "EVIDENCE_SUPPLEMENT":
        raise ValueError("supplement creation_kind must be EVIDENCE_SUPPLEMENT")

    target_ref = _required_text(raw.get("target_ref"), "target_ref")
    if not _REF.fullmatch(target_ref) or target_ref.startswith("/") or target_ref.endswith("/") or ".." in target_ref or "@{" in target_ref:
        raise ValueError("target_ref is invalid")
    candidate_sha = _required_text(raw.get("target_candidate_sha"), "target_candidate_sha").lower()
    if not _SHA40.fullmatch(candidate_sha):
        raise ValueError("target_candidate_sha must be full lowercase 40-hex")

    repo_fp = _required_text(raw.get("transport_repository_fingerprint"), "transport_repository_fingerprint").lower()
    if not repo_fp.startswith("sha256:") or not _SHA256.fullmatch(repo_fp[7:]):
        raise ValueError("transport_repository_fingerprint must be sha256:<64hex>")
    transport_branch = _required_text(raw.get("transport_starting_branch"), "transport_starting_branch")
    if not _REF.fullmatch(transport_branch) or transport_branch.startswith("/") or transport_branch.endswith("/") or ".." in transport_branch or "@{" in transport_branch:
        raise ValueError("transport_starting_branch is invalid")
    transport_head = _required_text(raw.get("transport_head_sha"), "transport_head_sha").lower()
    if not _SHA40.fullmatch(transport_head):
        raise ValueError("transport_head_sha must be full lowercase 40-hex")
    attested_at = _parse_time(_required_text(raw.get("transport_attested_at"), "transport_attested_at"))
    age = (now - attested_at).total_seconds()
    if age < -300 or age > _MAX_ATTESTATION_AGE_SECONDS:
        raise ValueError("transport source attestation is not fresh")

    evidence_root = _required_text(raw.get("evidence_root"), "evidence_root")
    if (
        not _SAFE_PATH.fullmatch(evidence_root)
        or evidence_root.startswith("/")
        or evidence_root.endswith("/")
        or ".." in evidence_root.split("/")
    ):
        raise ValueError("evidence_root must be a safe relative path")
    packet_sha = _required_text(raw.get("governed_packet_sha256"), "governed_packet_sha256").lower()
    evidence_sha = _required_text(raw.get("decoded_evidence_sha256"), "decoded_evidence_sha256").lower()
    if not _SHA256.fullmatch(packet_sha) or not _SHA256.fullmatch(evidence_sha):
        raise ValueError("governed/evidence digests must be lowercase SHA-256 hex")

    task_spec = raw.get("task_spec")
    if not isinstance(task_spec, Mapping):
        raise ValueError("supplement task_spec is required")
    task = _validate_task_spec(task_spec, target_ref=target_ref, candidate_sha=candidate_sha)
    return {
        "workstream": workstream,
        "role": role,
        "target_ref": target_ref,
        "candidate_sha": candidate_sha,
        "transport_repository_fingerprint": repo_fp,
        "transport_starting_branch": transport_branch,
        "transport_head_sha": transport_head,
        "transport_attested_at": attested_at.isoformat().replace("+00:00", "Z"),
        "evidence_root": evidence_root,
        "governed_packet_sha256": packet_sha,
        "decoded_evidence_sha256": evidence_sha,
        "task_spec": task,
    }


def _source_repository(source: Mapping[str, Any]) -> str:
    return str(legacy._source_repository(dict(source)) or "").strip()


def _resolve_unique_source(jules: Any, repository_fp: str) -> tuple[str, str] | None:
    digest = repository_fp[7:]
    matches: list[tuple[str, str]] = []
    for source in jules.list_sources(page_size=100):
        if not isinstance(source, Mapping):
            continue
        repository = _source_repository(source)
        name = str(source.get("name") or "").strip().strip("/")
        if not repository or not name:
            continue
        try:
            observed = repository_fingerprint(repository)
        except ValueError:
            continue
        if observed == digest:
            matches.append((name, repository))
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _sanitized_inventory(
    inventory: Sequence[Mapping[str, Any]], *, actual_repository: str, repository_alias: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in inventory:
        row = dict(item)
        if str(row.get("_source_repository") or "").casefold() == actual_repository.casefold():
            row["_source_repository"] = repository_alias
        result.append(row)
    return result


def _state_snapshot(store: Any, *, project: str, route: str, workstream: str) -> dict[str, Any]:
    lane_id = lineage_lane_id(project, route, workstream, _ALLOWED_ROLE)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        return {"generation": 0, "session_fingerprint": None}
    evidence = read.record.evidence_bindings or {}
    return {
        "generation": int(evidence.get("generation") or 0),
        "session_fingerprint": str(evidence.get("session_fingerprint") or "").strip() or None,
        "unknown_write_state": read.record.unknown_write_state,
        "action_in_flight": read.record.action_in_flight,
        "pending_initial_lineage_transition": evidence.get("pending_initial_lineage_transition"),
    }


def _projected_authority(authority: Mapping[str, Any], *, key: str, task_spec: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(authority)
    policy = dict(authority.get("generation_policy") or {}) if isinstance(authority.get("generation_policy"), Mapping) else {}
    policy["necessary_generation_authorized"] = True
    policy["generation_effect_authorized"] = True
    policy["authorized_initial_lineages"] = {
        key: {
            "authorized": True,
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "task_spec": dict(task_spec),
        }
    }
    projected["generation_policy"] = policy
    return projected


def _prompt(lane: Mapping[str, Any]) -> str:
    task = lane["task_spec"]
    candidate = str(lane["candidate_sha"])
    workstream = str(lane["workstream"])
    instructions = build_required_handoff_instructions(_ALLOWED_ROLE, workstream)
    instructions = instructions.replace('"candidate_sha": null', f'"candidate_sha": "{candidate}"').replace(
        '"reviewed_sha": null', f'"reviewed_sha": "{candidate}"'
    )
    transport_contract = {
        "evidence_supplement_only": True,
        "target_product_candidate_sha": candidate,
        "transport_checkout_expected_head": lane["transport_head_sha"],
        "evidence_root": lane["evidence_root"],
        "governed_packet_sha256": lane["governed_packet_sha256"],
        "decoded_evidence_sha256": lane["decoded_evidence_sha256"],
        "required_preinspection_checks": [
            "git rev-parse HEAD must equal transport_checkout_expected_head",
            "decode visual-evidence.webp.b64 under evidence_root and verify decoded SHA-256 equals decoded_evidence_sha256",
            "read CANARY_MANIFEST.json and TASK_CONTRACT.json under evidence_root",
        ],
        "evidence_semantics": [
            "The checked-out repository is a private evidence transport only; it is NOT the RP03 product repository.",
            "Do not inspect, modify, or report on unrelated transport-repository content.",
            "Do not replay repo-native product checks already completed by the original page assurance result.",
            "Inspect only the previously missing governed evidence. If the derived rendering is insufficient for a material comparison, return BLOCKED/UNKNOWN with MISSING_EVIDENCE rather than guessing.",
            "A supplement PASS means the previously missing evidence check passed; it does not independently re-adjudicate unrelated product checks.",
        ],
    }
    return (
        "Execute this Parent-governed RP03 evidence-supplement ASSURANCE task. READ_ONLY only; write_scope=[]. "
        "The transport checkout exists solely to make previously inaccessible governed evidence inspectable.\n\n"
        + json.dumps(dict(task), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n\nEVIDENCE_TRANSPORT_CONTRACT\n"
        + json.dumps(transport_contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n\n"
        + instructions
        + f"\nFor this supplement candidate_sha and reviewed_sha MUST both remain exactly {candidate}. "
        "If transport HEAD/hash verification fails or evidence cannot actually be inspected, do not claim PASS/FAIL; return BLOCKED/UNKNOWN and the exact evidence boundary."
    )


class _SanitizedCreateClient:
    def __init__(
        self,
        underlying: Any,
        *,
        actual_source_name: str,
        actual_repository: str,
        source_alias: str,
        repository_alias: str,
    ) -> None:
        self._underlying = underlying
        self._actual_source_name = actual_source_name
        self._actual_repository = actual_repository
        self._source_alias = source_alias
        self._repository_alias = repository_alias

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        source: str,
        starting_branch: str,
        require_plan_approval: bool = False,
        automation_mode: str = "AUTO_CREATE_PR",
        expected_repository: str,
    ) -> dict[str, Any]:
        if source != self._source_alias or expected_repository != self._repository_alias:
            raise ValueError("sanitized evidence transport binding mismatch")
        receipt = self._underlying.create_session(
            prompt=prompt,
            title=title,
            source=self._actual_source_name,
            starting_branch=starting_branch,
            require_plan_approval=require_plan_approval,
            automation_mode=automation_mode,
            expected_repository=self._actual_repository,
        )
        result = dict(receipt)
        result["source"] = self._source_alias
        result["repository"] = self._repository_alias
        result["private_source_identity_persisted"] = False
        return result


def _marker_matches(
    inventory: Sequence[Mapping[str, Any]], *, repository_alias: str, starting_branch: str, marker: str
) -> list[Mapping[str, Any]]:
    token = f"[{marker}]"
    return [
        item
        for item in inventory
        if str(item.get("_source_repository") or "") == repository_alias
        and str(item.get("sourceStartingBranch") or "") == starting_branch
        and token in str(item.get("title") or item.get("displayName") or "")
        and str(item.get("name") or "").strip()
    ]


def run_evidence_supplements(
    *,
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any],
    entries: Mapping[str, Any],
    store: Any,
    jules: Any,
    github: Any,
    inventory: Sequence[Mapping[str, Any]],
    provider_observation: Mapping[str, Any],
    actor: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    project = str(adapter.get("project") or "").strip().upper()
    route = str(adapter.get("route") or project).strip()
    target_repository = str(adapter.get("repository") or "").strip()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    results: list[dict[str, Any]] = []

    try:
        owner, repo = legacy._repo_parts(target_repository)
    except ValueError:
        return [{"decision": "EVIDENCE_SUPPLEMENT_TARGET_REPOSITORY_INVALID", "provider_write_attempted": False, "safe_to_blind_retry": False}]

    for raw_key, raw_lane in sorted(entries.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_lane, Mapping) or raw_lane.get("authorized") is not True:
            continue
        try:
            lane = _validate_lane(str(raw_key), raw_lane, now=current)
        except ValueError as exc:
            results.append(
                {
                    "authority_key": str(raw_key),
                    "decision": "EVIDENCE_SUPPLEMENT_AUTHORITY_INVALID",
                    "reason": str(exc),
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        exact = github.verify_exact_head(owner, repo, lane["target_ref"], lane["candidate_sha"])
        if not bool(exact.get("exact_head_match")):
            results.append(
                {
                    "workstream": lane["workstream"],
                    "role": _ALLOWED_ROLE,
                    "candidate_sha": lane["candidate_sha"],
                    "decision": "EVIDENCE_SUPPLEMENT_TARGET_CANDIDATE_MOVED",
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        try:
            source = _resolve_unique_source(jules, lane["transport_repository_fingerprint"])
        except _PROVIDER_READ_ERRORS as exc:
            results.append(
                {
                    "workstream": lane["workstream"],
                    "role": _ALLOWED_ROLE,
                    "decision": "EVIDENCE_SUPPLEMENT_SOURCE_READ_UNAVAILABLE",
                    "provider_read_error_category": getattr(exc, "category", type(exc).__name__),
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue
        except ProviderError as exc:
            results.append(
                {
                    "workstream": lane["workstream"],
                    "role": _ALLOWED_ROLE,
                    "decision": "EVIDENCE_SUPPLEMENT_SOURCE_READ_FAILED",
                    "provider_read_error_category": getattr(exc, "category", type(exc).__name__),
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue
        if source is None:
            results.append(
                {
                    "workstream": lane["workstream"],
                    "role": _ALLOWED_ROLE,
                    "decision": "EVIDENCE_SUPPLEMENT_UNIQUE_PRIVATE_SOURCE_REQUIRED",
                    "provider_write_attempted": False,
                    "private_source_identity_persisted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        actual_source_name, actual_repository = source
        repository_alias = lane["transport_repository_fingerprint"]
        source_alias = "sha256:" + hashlib.sha256(actual_source_name.encode("utf-8")).hexdigest()
        sanitized = _sanitized_inventory(
            inventory,
            actual_repository=actual_repository,
            repository_alias=repository_alias,
        )
        state = _state_snapshot(
            store,
            project=project,
            route=route,
            workstream=lane["workstream"],
        )
        projected_authority = _projected_authority(
            authority,
            key=f"{lane['workstream']}:{_ALLOWED_ROLE}",
            task_spec=lane["task_spec"],
        )
        projected_adapter = dict(adapter)
        projected_adapter["repository"] = repository_alias
        effective = resolve_execution_policy(
            adapter=projected_adapter,
            governed_authority=projected_authority,
            provider_observation=provider_observation,
            state_snapshot=state,
        ).to_dict()

        if state.get("unknown_write_state") and isinstance(state.get("pending_initial_lineage_transition"), Mapping):
            effect = reconcile_unknown_initial_lineage(
                store,
                project=project,
                route=route,
                workstream=lane["workstream"],
                role=_ALLOWED_ROLE,
                inventory=sanitized,
                authority_event_id=str(authority.get("authority_event_id") or ""),
                policy_provenance=effective.get("provenance") if isinstance(effective.get("provenance"), Mapping) else {},
            )
        else:
            transition_key = initial_lineage_transition_key(
                project=project,
                route=route,
                workstream=lane["workstream"],
                role=_ALLOWED_ROLE,
                candidate_sha=lane["candidate_sha"],
                initial_task_spec=lane["task_spec"],
            )
            matches = _marker_matches(
                sanitized,
                repository_alias=repository_alias,
                starting_branch=lane["transport_starting_branch"],
                marker=transition_key[:12],
            )
            if int(state.get("generation") or 0) == 0 and matches:
                effect = {
                    "decision": "EVIDENCE_SUPPLEMENT_EXISTING_PROVIDER_MARKER_REQUIRES_ADJUDICATION",
                    "provider_write_attempted": False,
                    "match_count": len(matches),
                    "safe_to_blind_retry": False,
                }
            else:
                client = _SanitizedCreateClient(
                    jules,
                    actual_source_name=actual_source_name,
                    actual_repository=actual_repository,
                    source_alias=source_alias,
                    repository_alias=repository_alias,
                )
                effect = execute_initial_lineage_generation(
                    store,
                    client,
                    adapter=projected_adapter,
                    authority=projected_authority,
                    transport_actor=actor,
                    current_policy=effective,
                    project=project,
                    route=route,
                    workstream=lane["workstream"],
                    role=_ALLOWED_ROLE,
                    task_spec=lane["task_spec"],
                    prompt=_prompt(lane),
                    title=f"{project} {lane['workstream']} ASSURANCE EVIDENCE SUPPLEMENT",
                    source_name=source_alias,
                    starting_branch=lane["transport_starting_branch"],
                    repository=repository_alias,
                    candidate_sha=lane["candidate_sha"],
                    active_duplicate_absent=not matches,
                    exact_repository_binding=True,
                    exact_starting_ref_binding=True,
                )

        results.append(
            {
                "workstream": lane["workstream"],
                "role": _ALLOWED_ROLE,
                "candidate_sha": lane["candidate_sha"],
                "creation_kind": "EVIDENCE_SUPPLEMENT",
                "transport_repository_fingerprint": repository_alias,
                "transport_starting_branch": lane["transport_starting_branch"],
                "transport_head_sha": lane["transport_head_sha"],
                "governed_packet_sha256": lane["governed_packet_sha256"],
                "decoded_evidence_sha256": lane["decoded_evidence_sha256"],
                "private_source_identity_persisted": False,
                "current_policy": effective,
                "effect": effect,
            }
        )
    return results
