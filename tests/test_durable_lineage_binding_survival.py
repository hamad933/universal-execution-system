from __future__ import annotations

import tempfile
from pathlib import Path

from ues.lineage_registry import lineage_lane_id, upsert_lineage_observation
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord

FP3 = "a" * 64
FP4 = "b" * 64


def _store(evidence):
    root = tempfile.TemporaryDirectory()
    store = DeterministicFileStateStore(Path(root.name) / "state.json")
    store.initialize()
    lane = lineage_lane_id("RP04", "RP04", "RP04-IPA-S01-001", "REVIEWER")
    record = WorkstreamRuntimeRecord(
        lane_id=lane,
        project="RP04",
        route="RP04",
        workstream_id="LINEAGE::RP04-IPA-S01-001::REVIEWER",
        evidence_bindings=dict(evidence),
    )
    store.compare_and_swap_workstream(lane, 0, record)
    return root, store, lane


def test_unbound_observation_preserves_proven_durable_fingerprint():
    root, store, lane = _store(
        {
            "generation": 3,
            "session_fingerprint": FP3,
            "known_session_fingerprints": [FP3],
            "replacement_reason": "STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
        }
    )
    try:
        result = upsert_lineage_observation(
            store,
            project="RP04",
            route="RP04",
            workstream="RP04-IPA-S01-001",
            role="REVIEWER",
            binding={"status": "UNBOUND", "reason": "PROVIDER_READ_TEMPORARILY_INCOMPLETE"},
            policy={"known_session_fingerprints": [FP3], "provider_starting_branch": "main"},
        )
        read = store.read_workstream(lane)
        evidence = read.record.evidence_bindings
        assert result["binding_status"] == "UNBOUND"
        assert evidence["generation"] == 3
        assert evidence["session_fingerprint"] == FP3
        assert evidence["binding_status"] == "UNBOUND"
    finally:
        root.cleanup()


def test_exact_governed_rebind_does_not_increment_existing_generation():
    root, store, lane = _store(
        {
            "generation": 3,
            "session_fingerprint": None,
            "known_session_fingerprints": [FP3],
        }
    )
    try:
        result = upsert_lineage_observation(
            store,
            project="RP04",
            route="RP04",
            workstream="RP04-IPA-S01-001",
            role="REVIEWER",
            binding={
                "status": "PROVEN",
                "reason": "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_DRIFT",
                "session_fingerprint": FP3,
                "provider_state": "COMPLETED",
                "provider_starting_branch_metadata_drift": True,
                "observed_provider_starting_branch": "provider/generated-branch",
                "session": {
                    "_source_repository": "hamad933/Real-Estate-Assets-Control-",
                    "sourceStartingBranch": "provider/generated-branch",
                },
            },
            policy={"known_session_fingerprints": [FP3], "provider_starting_branch": "main"},
        )
        read = store.read_workstream(lane)
        evidence = read.record.evidence_bindings
        assert result["generation"] == 3
        assert evidence["generation"] == 3
        assert evidence["session_fingerprint"] == FP3
        assert evidence["replacement_reason"] == "DURABLE_FINGERPRINT_REBOUND"
        assert evidence["provider_starting_branch_metadata_drift"] is True
    finally:
        root.cleanup()


def test_genuine_new_exact_fingerprint_still_advances_generation():
    root, store, lane = _store(
        {
            "generation": 3,
            "session_fingerprint": FP3,
            "known_session_fingerprints": [FP3, FP4],
        }
    )
    try:
        result = upsert_lineage_observation(
            store,
            project="RP04",
            route="RP04",
            workstream="RP04-IPA-S01-001",
            role="REVIEWER",
            binding={
                "status": "PROVEN",
                "reason": "EXACT_GOVERNED_LINEAGE_BINDING",
                "session_fingerprint": FP4,
                "provider_state": "COMPLETED",
                "session": {
                    "_source_repository": "hamad933/Real-Estate-Assets-Control-",
                    "sourceStartingBranch": "main",
                },
            },
            policy={"known_session_fingerprints": [FP3, FP4], "provider_starting_branch": "main"},
        )
        read = store.read_workstream(lane)
        assert result["generation"] == 4
        assert read.record.evidence_bindings["session_fingerprint"] == FP4
        assert read.record.evidence_bindings["replacement_reason"] == "PROVIDER_SESSION_GENERATION_CHANGED"
    finally:
        root.cleanup()
