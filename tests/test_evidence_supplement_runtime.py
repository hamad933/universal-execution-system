from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone

from ues.evidence_supplement_runtime import (
    _SanitizedCreateClient,
    _prompt,
    _resolve_unique_source,
    _sanitized_inventory,
    _validate_lane,
    evidence_supplement_entries,
)
from ues.jules_source_probe import repository_fingerprint


CANDIDATE = "06d7e80af27232f416940d04dffe4a325b01e14d"
TRANSPORT_HEAD = "1" * 40
PACKET_SHA = "2" * 64
EVIDENCE_SHA = "3" * 64


def lane(repository_fp: str) -> dict:
    return {
        "authorized": True,
        "creation_kind": "EVIDENCE_SUPPLEMENT",
        "target_ref": "main",
        "target_candidate_sha": CANDIDATE,
        "transport_repository_fingerprint": f"sha256:{repository_fp}",
        "transport_starting_branch": "ues-transport/rp03-evidence-supplement-canary",
        "transport_head_sha": TRANSPORT_HEAD,
        "transport_attested_at": "2026-08-27T14:00:00Z",
        "evidence_root": "rp03-evidence-supplement/RP03-S02",
        "governed_packet_sha256": PACKET_SHA,
        "decoded_evidence_sha256": EVIDENCE_SHA,
        "task_spec": {
            "objective": "Inspect only the previously missing evidence.",
            "exact_baseline": f"main@{CANDIDATE}",
            "write_scope": [],
            "prohibited_scope": ["mutation"],
            "validation": ["verify hashes"],
            "evidence": ["governed packet"],
            "handoff": "return structured supplement result",
            "stop_gate": "RESULT_RETURNED",
        },
    }


class FakeSources:
    def __init__(self, sources):
        self.sources = sources

    def list_sources(self, *, page_size=100):
        self.page_size = page_size
        return list(self.sources)


class FakeCreate:
    def __init__(self):
        self.kwargs = None

    def create_session(self, **kwargs):
        self.kwargs = kwargs
        return {
            "provider": "JULES",
            "operation": "createSession",
            "session": "sessions/private-raw-id",
            "state": "QUEUED",
            "source": kwargs["source"],
            "repository": kwargs["expected_repository"],
            "starting_branch": kwargs["starting_branch"],
            "authoritative_readback": True,
            "safe_to_blind_retry": False,
        }


class EvidenceSupplementRuntimeTests(unittest.TestCase):
    def test_policy_is_separate_from_original_initial_lineages(self) -> None:
        authority = {"generation_policy": {"evidence_supplement_lineages": {"A:ASSURANCE": {"authorized": True}}}}
        self.assertIn("A:ASSURANCE", evidence_supplement_entries(authority))
        self.assertNotIn("authorized_initial_lineages", authority["generation_policy"])

    def test_lane_requires_fresh_exact_target_and_read_only_contract(self) -> None:
        raw = lane(repository_fingerprint("example/private-evidence"))
        parsed = _validate_lane(
            "RP03-IPA-S02-EVIDENCE-SUPPLEMENT:ASSURANCE",
            raw,
            now=datetime(2026, 8, 27, 14, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(parsed["candidate_sha"], CANDIDATE)
        self.assertEqual(parsed["task_spec"]["write_scope"], [])
        raw["task_spec"]["write_scope"] = ["src/"]
        with self.assertRaises(ValueError):
            _validate_lane(
                "RP03-IPA-S02-EVIDENCE-SUPPLEMENT:ASSURANCE",
                raw,
                now=datetime(2026, 8, 27, 14, 10, tzinfo=timezone.utc),
            )

    def test_stale_transport_attestation_fails_closed(self) -> None:
        raw = lane(repository_fingerprint("example/private-evidence"))
        with self.assertRaisesRegex(ValueError, "not fresh"):
            _validate_lane(
                "RP03-IPA-S02-EVIDENCE-SUPPLEMENT:ASSURANCE",
                raw,
                now=datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
            )

    def test_unique_private_source_is_resolved_by_hash_only(self) -> None:
        wanted = repository_fingerprint("Example/Private-Evidence")
        client = FakeSources(
            [
                {"repository": "Example/Private-Evidence", "name": "sources/private-id"},
                {"repository": "Example/Other", "name": "sources/other-id"},
            ]
        )
        self.assertEqual(
            _resolve_unique_source(client, f"sha256:{wanted}"),
            ("sources/private-id", "Example/Private-Evidence"),
        )
        duplicate = FakeSources(
            [
                {"repository": "Example/Private-Evidence", "name": "sources/one"},
                {"repository": "Example/Private-Evidence", "name": "sources/two"},
            ]
        )
        self.assertIsNone(_resolve_unique_source(duplicate, f"sha256:{wanted}"))

    def test_inventory_reconciliation_view_replaces_private_repo_with_hash_alias(self) -> None:
        alias = "sha256:" + repository_fingerprint("Example/Private-Evidence")
        rows = _sanitized_inventory(
            [{"_source_repository": "Example/Private-Evidence", "name": "sessions/raw"}],
            actual_repository="Example/Private-Evidence",
            repository_alias=alias,
        )
        self.assertEqual(rows[0]["_source_repository"], alias)
        self.assertNotIn("Private-Evidence", repr(rows))

    def test_create_wrapper_uses_real_source_only_in_memory_and_returns_aliases(self) -> None:
        underlying = FakeCreate()
        repo_alias = "sha256:" + repository_fingerprint("Example/Private-Evidence")
        source_alias = "sha256:" + hashlib.sha256(b"sources/private-id").hexdigest()
        client = _SanitizedCreateClient(
            underlying,
            actual_source_name="sources/private-id",
            actual_repository="Example/Private-Evidence",
            source_alias=source_alias,
            repository_alias=repo_alias,
        )
        receipt = client.create_session(
            prompt="review evidence",
            title="supplement",
            source=source_alias,
            starting_branch="ues-transport/canary",
            expected_repository=repo_alias,
        )
        self.assertEqual(underlying.kwargs["source"], "sources/private-id")
        self.assertEqual(underlying.kwargs["expected_repository"], "Example/Private-Evidence")
        self.assertEqual(receipt["source"], source_alias)
        self.assertEqual(receipt["repository"], repo_alias)
        self.assertFalse(receipt["private_source_identity_persisted"])
        self.assertNotIn("Private-Evidence", repr(receipt))
        self.assertNotIn("private-id", repr(receipt))

    def test_prompt_binds_product_candidate_and_transport_integrity_without_private_repo_name(self) -> None:
        parsed = _validate_lane(
            "RP03-IPA-S02-EVIDENCE-SUPPLEMENT:ASSURANCE",
            lane(repository_fingerprint("example/private-evidence")),
            now=datetime(2026, 8, 27, 14, 10, tzinfo=timezone.utc),
        )
        text = _prompt(parsed)
        self.assertIn(CANDIDATE, text)
        self.assertIn(TRANSPORT_HEAD, text)
        self.assertIn(EVIDENCE_SHA, text)
        self.assertIn("MISSING_EVIDENCE", text)
        self.assertIn("transport only", text)
        self.assertNotIn("example/private-evidence", text.casefold())


if __name__ == "__main__":
    unittest.main()
