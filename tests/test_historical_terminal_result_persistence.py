from __future__ import annotations

import unittest
from dataclasses import dataclass

from ues.lineage_registry import lineage_lane_id
from ues.state_store import StateVersionConflict, WorkstreamRuntimeRecord
from ues.terminal_recovery import TERMINAL_RESULT_KEY, persist_terminal_result


HISTORICAL_KEY = "historical_terminal_results_v1"
CURRENT_FP = "c" * 64
HISTORICAL_FP = "b" * 64


@dataclass
class _Read:
    status: str
    version: int = 0
    record: WorkstreamRuntimeRecord | None = None
    reason: str | None = None


class FakeStore:
    def __init__(self, lane_id: str, record: WorkstreamRuntimeRecord):
        self.lane_id = lane_id
        self.version = 1
        self.record = WorkstreamRuntimeRecord.from_dict(record.to_dict())
        self.cas_calls = 0

    def discover_lane_ids(self):
        return [self.lane_id]

    def read_workstream(self, lane_id):
        if lane_id != self.lane_id:
            return _Read("MISSING")
        return _Read("OK", self.version, WorkstreamRuntimeRecord.from_dict(self.record.to_dict()))

    def compare_and_swap_workstream(self, lane_id, expected_version, record):
        self.cas_calls += 1
        if lane_id != self.lane_id or expected_version != self.version:
            raise StateVersionConflict("test conflict")
        self.version += 1
        self.record = WorkstreamRuntimeRecord.from_dict(record.to_dict())
        return _Read("OK", self.version, WorkstreamRuntimeRecord.from_dict(self.record.to_dict()))


def _store():
    lane_id = lineage_lane_id("RP02", "RP02", "S02", "REVIEWER")
    record = WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project="RP02",
        route="RP02",
        workstream_id="LINEAGE::S02::REVIEWER",
        evidence_bindings={
            "role": "REVIEWER",
            "workstream": "S02",
            "generation": 3,
            "session_fingerprint": CURRENT_FP,
            "previous_session_fingerprint": HISTORICAL_FP,
            "current_candidate_sha": "a" * 40,
            "binding_status": "PROVEN",
            "raw_session_id_persisted": False,
        },
        authority_provenance={"authority_event_id": "AUTH-1"},
    )
    return lane_id, FakeStore(lane_id, record)


def _historical_result():
    return {
        "schema_version": "1.0",
        "project": "RP02",
        "route": "RP02",
        "logical_workstream": "S02",
        "role": "REVIEWER",
        "generation": 2,
        "session_fingerprint": HISTORICAL_FP,
        "repository": "hamad933/Enterprise-Operations-Control",
        "status": "COMPLETE",
        "verdict": "PASS",
        "reviewed_sha": "a" * 40,
        "candidate_sha": None,
        "finding_count": 0,
        "findings": [],
        "context_state": "OK",
        "freshness_status": "FRESH",
        "result_state": "PARENT_CONSUMABLE",
        "result_fingerprint": "historical-result-2",
        "raw_activity_content_persisted": False,
        "raw_session_id_persisted": False,
    }


def _historical_lineage(lane_id):
    return {
        "lane_id": lane_id,
        "role": "REVIEWER",
        "workstream": "S02",
        "generation": 2,
        "identity_recovery_source": "PREVIOUS_SESSION_FINGERPRINT",
    }


class HistoricalTerminalResultPersistenceTests(unittest.TestCase):
    def test_exact_historical_result_persists_without_rewinding_current_lineage(self):
        lane_id, store = _store()
        outcome = persist_terminal_result(
            store,
            result=_historical_result(),
            lineage=_historical_lineage(lane_id),
        )
        self.assertEqual(outcome["state"], "HISTORICAL_TERMINAL_RESULT_PERSISTED")
        self.assertTrue(outcome["authoritative_readback"])
        saved = store.record.evidence_bindings
        self.assertEqual(saved["generation"], 3)
        self.assertEqual(saved["session_fingerprint"], CURRENT_FP)
        self.assertNotIn(TERMINAL_RESULT_KEY, saved)
        history = saved[HISTORICAL_KEY]
        self.assertEqual(len(history), 1)
        entry = next(iter(history.values()))
        self.assertEqual(entry["generation"], 2)
        self.assertEqual(entry["session_fingerprint"], HISTORICAL_FP)
        self.assertFalse(entry["safe_to_blind_retry"])

    def test_exact_historical_replay_is_idempotent(self):
        lane_id, store = _store()
        first = persist_terminal_result(
            store,
            result=_historical_result(),
            lineage=_historical_lineage(lane_id),
        )
        second = persist_terminal_result(
            store,
            result=_historical_result(),
            lineage=_historical_lineage(lane_id),
        )
        self.assertEqual(first["state"], "HISTORICAL_TERMINAL_RESULT_PERSISTED")
        self.assertEqual(second["state"], "HISTORICAL_TERMINAL_RESULT_ALREADY_PERSISTED")
        self.assertEqual(store.cas_calls, 1)

    def test_current_generation_semantics_remain_terminal_result_v1(self):
        lane_id, store = _store()
        current = _historical_result()
        current["generation"] = 3
        current["session_fingerprint"] = CURRENT_FP
        current["result_fingerprint"] = "current-result-3"
        lineage = {
            "lane_id": lane_id,
            "role": "REVIEWER",
            "workstream": "S02",
            "generation": 3,
        }
        outcome = persist_terminal_result(store, result=current, lineage=lineage)
        self.assertEqual(outcome["state"], "TERMINAL_RESULT_PERSISTED")
        self.assertIn(TERMINAL_RESULT_KEY, store.record.evidence_bindings)
        self.assertNotIn(HISTORICAL_KEY, store.record.evidence_bindings)


if __name__ == "__main__":
    unittest.main()
