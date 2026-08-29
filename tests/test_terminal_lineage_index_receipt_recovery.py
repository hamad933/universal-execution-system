from types import SimpleNamespace

from ues.terminal_results import lineage_index


FP = "a" * 64


class FakeStore:
    def __init__(self, record):
        self.record = record

    def discover_lane_ids(self):
        return ["lane-1"]

    def read_workstream(self, lane_id):
        assert lane_id == "lane-1"
        return SimpleNamespace(status="OK", record=self.record)


def _record(*, receipt_state="CONFIRMED", current_fp=None):
    return SimpleNamespace(
        project="RP04",
        route="RP04",
        workstream_id="LINEAGE::RP04-IPA-S01-001::REVIEWER",
        evidence_bindings={
            "role": "REVIEWER",
            "workstream": "RP04-IPA-S01-001",
            "generation": 1,
            "session_fingerprint": current_fp,
            "current_candidate_sha": "53c7ee4801ed76983b45a8e245f41dbe268a6ee1",
        },
        operation_receipt={
            "state": receipt_state,
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "generation": 1,
            "starting_branch": "main",
            "post_condition": {
                "observed": True,
                "evidence": {
                    "session_fingerprint": FP,
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "generation": 1,
                    "repository": "hamad933/Real-Estate-Assets-Control-",
                    "starting_branch": "main",
                },
            },
        },
    )


def test_lineage_index_recovers_confirmed_creation_receipt_fingerprint():
    index = lineage_index(FakeStore(_record()), project="RP04", route="RP04")

    assert list(index) == [FP]
    assert index[FP][0]["workstream"] == "RP04-IPA-S01-001"
    assert index[FP][0]["role"] == "REVIEWER"
    assert index[FP][0]["generation"] == 1
    assert index[FP][0]["identity_recovery_source"] == "CONFIRMED_CREATION_RECEIPT"


def test_lineage_index_fails_closed_when_same_generation_proofs_disagree():
    current = "b" * 64
    index = lineage_index(FakeStore(_record(current_fp=current)), project="RP04", route="RP04")

    assert index == {}


def test_lineage_index_deduplicates_matching_same_generation_proofs():
    index = lineage_index(FakeStore(_record(current_fp=FP)), project="RP04", route="RP04")

    assert list(index) == [FP]
    assert len(index[FP]) == 1
    assert index[FP][0]["identity_recovery_source"] == "EVIDENCE_BINDINGS"


def test_lineage_index_rejects_nonconfirmed_receipt_fingerprint():
    index = lineage_index(FakeStore(_record(receipt_state="IN_FLIGHT")), project="RP04", route="RP04")

    assert index == {}
