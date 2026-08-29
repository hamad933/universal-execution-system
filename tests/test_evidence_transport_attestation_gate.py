from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from ues.evidence_transport_attestation_gate import (
    ACCEPTED_ATTESTATIONS_POLICY_KEY,
    ATTESTATION_ACTION,
    ATTESTATION_OPERATION_PREFIX,
    ATTESTATION_RESULT,
    attestation_operation_key,
    enforce_authority_attestations,
    validate_attestation_record,
)


REPO_FP = "sha256:" + "a" * 64
HEAD = "b" * 40
EVIDENCE_SHA = "c" * 64
AUTHORITY_KEY = "RP03-IPA-S02-EVIDENCE-SUPPLEMENT:ASSURANCE"


def lane() -> dict:
    return {
        "authorized": True,
        "transport_repository_fingerprint": REPO_FP,
        "transport_head_sha": HEAD,
        "decoded_evidence_sha256": EVIDENCE_SHA,
    }


def digest() -> str:
    return hashlib.sha256(f"{REPO_FP}|{HEAD}|{EVIDENCE_SHA}".encode("utf-8")).hexdigest()


def record(**overrides):
    receipt = {
        "result": ATTESTATION_RESULT,
        "transport_repository_fingerprint": REPO_FP,
        "transport_head_sha": HEAD,
        "decoded_evidence_sha256": EVIDENCE_SHA,
        "decoded_evidence_byte_count": 14128,
        "provider_mutation": False,
        "private_source_identity_persisted": False,
    }
    receipt.update(overrides.pop("receipt", {}))
    values = {
        "operation_key": ATTESTATION_OPERATION_PREFIX + digest(),
        "action": ATTESTATION_ACTION,
        "state": "CONFIRMED",
        "request_digest": digest(),
        "receipt": receipt,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def authority(*, accept: bool) -> dict:
    policy = {"evidence_supplement_lineages": {AUTHORITY_KEY: lane()}}
    if accept:
        policy[ACCEPTED_ATTESTATIONS_POLICY_KEY] = {AUTHORITY_KEY: attestation_operation_key(lane())}
    return {
        "authority_event_id": "RP03-CE-TEST",
        "source_id": "drive-current-state-test",
        "generation_policy": policy,
    }


class FakeStore:
    def __init__(self, value=None, *, status="OK"):
        self.value = value
        self.status = status
        self.requested = []

    def read_operation(self, key):
        self.requested.append(key)
        return SimpleNamespace(status=self.status, record=self.value)


class EvidenceTransportAttestationGateTests(unittest.TestCase):
    def test_operation_key_is_bound_to_private_fingerprint_head_and_decoded_digest(self) -> None:
        key = attestation_operation_key(lane())
        self.assertEqual(key, ATTESTATION_OPERATION_PREFIX + digest())
        changed = lane()
        changed["transport_head_sha"] = "d" * 40
        self.assertNotEqual(attestation_operation_key(changed), key)

    def test_confirmed_durable_receipt_passes_without_private_identity(self) -> None:
        proof = validate_attestation_record(lane(), record())
        self.assertEqual(proof["decoded_evidence_byte_count"], 14128)
        self.assertFalse(proof["provider_mutation"])
        self.assertFalse(proof["private_source_identity_persisted"])
        self.assertNotIn("private-evidence", repr(proof).casefold())

    def test_current_state_must_accept_exact_operation_before_store_receipt_is_considered(self) -> None:
        store = FakeStore(record())
        with self.assertRaisesRegex(RuntimeError, "same governed Current State has not accepted"):
            enforce_authority_attestations(authority(accept=False), store)
        self.assertEqual(store.requested, [])

    def test_request_local_metadata_cannot_substitute_for_state_store_receipt(self) -> None:
        store = FakeStore(None, status="MISSING")
        with self.assertRaisesRegex(RuntimeError, "independent byte-attestation receipt is required"):
            enforce_authority_attestations(authority(accept=True), store)
        self.assertEqual(store.requested, [attestation_operation_key(lane())])

    def test_same_current_state_acceptance_plus_confirmed_receipt_passes(self) -> None:
        result = enforce_authority_attestations(authority(accept=True), FakeStore(record()))
        self.assertEqual(result["result"], "EVIDENCE_TRANSPORT_ATTESTATION_GATE_PASS")
        self.assertEqual(result["supplement_lanes_checked"], 1)
        self.assertEqual(result["authority_event_id"], "RP03-CE-TEST")
        self.assertEqual(result["attestations"][0]["decoded_evidence_byte_count"], 14128)
        self.assertFalse(result["provider_mutation"])

    def test_receipt_must_match_exact_transport_head_and_digest(self) -> None:
        bad_head = record(receipt={"transport_head_sha": "d" * 40})
        with self.assertRaisesRegex(ValueError, "transport head mismatch"):
            validate_attestation_record(lane(), bad_head)
        bad_digest = record(receipt={"decoded_evidence_sha256": "e" * 64})
        with self.assertRaisesRegex(ValueError, "decoded evidence digest mismatch"):
            validate_attestation_record(lane(), bad_digest)

    def test_receipt_must_be_trusted_confirmed_non_mutating_byte_verifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "not CONFIRMED"):
            validate_attestation_record(lane(), record(state="UNKNOWN"))
        with self.assertRaisesRegex(ValueError, "trusted byte-verifier action"):
            validate_attestation_record(lane(), record(action="controller-self-assertion"))
        with self.assertRaisesRegex(ValueError, "positive decoded byte count"):
            validate_attestation_record(lane(), record(receipt={"decoded_evidence_byte_count": 0}))
        with self.assertRaisesRegex(ValueError, "provider_mutation=false"):
            validate_attestation_record(lane(), record(receipt={"provider_mutation": True}))
        with self.assertRaisesRegex(ValueError, "private source identity"):
            validate_attestation_record(lane(), record(receipt={"private_source_identity_persisted": True}))

    def test_no_supplement_lanes_is_noop_pass(self) -> None:
        result = enforce_authority_attestations({"generation_policy": {}}, FakeStore())
        self.assertEqual(result["result"], "EVIDENCE_TRANSPORT_ATTESTATION_GATE_NOT_REQUIRED")
        self.assertEqual(result["supplement_lanes_checked"], 0)
        self.assertFalse(result["provider_mutation"])


if __name__ == "__main__":
    unittest.main()
