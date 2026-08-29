from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

from .live_runtime import build_live_state_store

SCHEMA_VERSION = "1.1"
SUPPLEMENT_POLICY_KEY = "evidence_supplement_lineages"
ACCEPTED_ATTESTATIONS_POLICY_KEY = "accepted_evidence_transport_attestations"
ATTESTATION_ACTION = "evidence-transport-byte-attestation"
ATTESTATION_RESULT = "EVIDENCE_TRANSPORT_BYTE_ATTESTATION_PASS"
ATTESTATION_OPERATION_PREFIX = "ues-v2:evidence-transport-attestation:"


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _binding_digest(lane: Mapping[str, Any]) -> str:
    repository_fp = _required_text(
        lane.get("transport_repository_fingerprint"),
        "transport_repository_fingerprint",
    ).lower()
    transport_head = _required_text(lane.get("transport_head_sha"), "transport_head_sha").lower()
    evidence_sha = _required_text(
        lane.get("decoded_evidence_sha256"),
        "decoded_evidence_sha256",
    ).lower()
    canonical = f"{repository_fp}|{transport_head}|{evidence_sha}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attestation_operation_key(lane: Mapping[str, Any]) -> str:
    return ATTESTATION_OPERATION_PREFIX + _binding_digest(lane)


def _generation_policy(authority: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = authority.get("generation_policy")
    return policy if isinstance(policy, Mapping) else {}


def _supplement_entries(authority: Mapping[str, Any]) -> Mapping[str, Any]:
    entries = _generation_policy(authority).get(SUPPLEMENT_POLICY_KEY)
    return entries if isinstance(entries, Mapping) else {}


def _accepted_attestations(authority: Mapping[str, Any]) -> Mapping[str, Any]:
    accepted = _generation_policy(authority).get(ACCEPTED_ATTESTATIONS_POLICY_KEY)
    return accepted if isinstance(accepted, Mapping) else {}


def validate_attestation_record(lane: Mapping[str, Any], record: Any) -> dict[str, Any]:
    expected_digest = _binding_digest(lane)
    expected_key = ATTESTATION_OPERATION_PREFIX + expected_digest
    if str(getattr(record, "operation_key", "") or "") != expected_key:
        raise ValueError("attestation operation key does not match exact transport binding")
    if str(getattr(record, "action", "") or "") != ATTESTATION_ACTION:
        raise ValueError("attestation operation action is not the trusted byte-verifier action")
    if str(getattr(record, "state", "") or "").upper() != "CONFIRMED":
        raise ValueError("attestation operation is not CONFIRMED")
    if str(getattr(record, "request_digest", "") or "").lower() != expected_digest:
        raise ValueError("attestation request digest does not match exact transport binding")

    receipt = getattr(record, "receipt", None)
    if not isinstance(receipt, Mapping):
        raise ValueError("attestation operation has no durable receipt")
    if str(receipt.get("result") or "") != ATTESTATION_RESULT:
        raise ValueError("attestation receipt does not prove byte verification PASS")

    repository_fp = _required_text(
        lane.get("transport_repository_fingerprint"),
        "transport_repository_fingerprint",
    ).lower()
    transport_head = _required_text(lane.get("transport_head_sha"), "transport_head_sha").lower()
    evidence_sha = _required_text(
        lane.get("decoded_evidence_sha256"),
        "decoded_evidence_sha256",
    ).lower()
    if str(receipt.get("transport_repository_fingerprint") or "").lower() != repository_fp:
        raise ValueError("attestation receipt repository fingerprint mismatch")
    if str(receipt.get("transport_head_sha") or "").lower() != transport_head:
        raise ValueError("attestation receipt transport head mismatch")
    if str(receipt.get("decoded_evidence_sha256") or "").lower() != evidence_sha:
        raise ValueError("attestation receipt decoded evidence digest mismatch")

    byte_count = receipt.get("decoded_evidence_byte_count")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
        raise ValueError("attestation receipt must include a positive decoded byte count")
    if receipt.get("provider_mutation") is not False:
        raise ValueError("attestation receipt must prove provider_mutation=false")
    if receipt.get("private_source_identity_persisted") is not False:
        raise ValueError("attestation receipt must prove private source identity was not persisted")

    return {
        "schema_version": SCHEMA_VERSION,
        "operation_key": expected_key,
        "request_digest": expected_digest,
        "transport_repository_fingerprint": repository_fp,
        "transport_head_sha": transport_head,
        "decoded_evidence_sha256": evidence_sha,
        "decoded_evidence_byte_count": byte_count,
        "provider_mutation": False,
        "private_source_identity_persisted": False,
    }


def enforce_authority_attestations(authority: Mapping[str, Any], store: Any) -> dict[str, Any]:
    entries = _supplement_entries(authority)
    if not any(isinstance(lane, Mapping) and lane.get("authorized") is True for lane in entries.values()):
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "EVIDENCE_TRANSPORT_ATTESTATION_GATE_NOT_REQUIRED",
            "supplement_lanes_checked": 0,
            "attestations": [],
            "provider_mutation": False,
        }

    checked: list[dict[str, Any]] = []
    accepted = _accepted_attestations(authority)
    authority_event_id = _required_text(authority.get("authority_event_id"), "authority_event_id")
    source_id = _required_text(authority.get("source_id"), "source_id")
    for raw_key, raw_lane in sorted(entries.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_lane, Mapping) or raw_lane.get("authorized") is not True:
            continue
        key = attestation_operation_key(raw_lane)
        accepted_key = str(accepted.get(str(raw_key)) or "").strip()
        if accepted_key != key:
            raise RuntimeError(
                f"{raw_key}: the same governed Current State has not accepted the exact byte-attestation operation"
            )
        read = store.read_operation(key)
        if read.status != "OK" or read.record is None:
            raise RuntimeError(
                f"{raw_key}: independent byte-attestation receipt is required before evidence-supplement provider effects"
            )
        proof = validate_attestation_record(raw_lane, read.record)
        proof["authority_key"] = str(raw_key)
        proof["accepted_by_authority_event_id"] = authority_event_id
        proof["accepted_by_source_id"] = source_id
        checked.append(proof)
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "EVIDENCE_TRANSPORT_ATTESTATION_GATE_PASS",
        "authority_event_id": authority_event_id,
        "source_id": source_id,
        "supplement_lanes_checked": len(checked),
        "attestations": checked,
        "provider_mutation": False,
    }


def main() -> None:
    raw = str(os.environ.get("UES_CURRENT_AUTHORITY_JSON") or "").strip()
    if not raw:
        raise SystemExit("UES_CURRENT_AUTHORITY_JSON is required")
    try:
        authority = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"UES_CURRENT_AUTHORITY_JSON is invalid JSON: {exc}") from exc
    if not isinstance(authority, Mapping):
        raise SystemExit("UES_CURRENT_AUTHORITY_JSON must be a JSON object")
    result = enforce_authority_attestations(authority, build_live_state_store())
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
