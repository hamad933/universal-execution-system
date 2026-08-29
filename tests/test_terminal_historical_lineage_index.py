from types import SimpleNamespace

from ues.terminal_results import lineage_index


FP1 = "1" * 64
FP2 = "2" * 64
FP3 = "3" * 64


class FakeStore:
    def __init__(self, record):
        self.record = record

    def discover_lane_ids(self):
        return ["lane-1"]

    def read_workstream(self, lane_id):
        assert lane_id == "lane-1"
        return SimpleNamespace(status="OK", record=self.record)


def _record():
    return SimpleNamespace(
        project="RP02",
        route="RP02",
        workstream_id="LINEAGE::RP02-IPA-S01-001::REVIEWER",
        evidence_bindings={
            "role": "REVIEWER",
            "workstream": "RP02-IPA-S01-001",
            "generation": 3,
            "session_fingerprint": FP3,
            "previous_session_fingerprint": FP2,
            "current_candidate_sha": "5fed7c1b4648dd176907561283c3d716f159efc8",
        },
        operation_receipt={
            "state": "CONFIRMED",
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "generation": 1,
            "starting_branch": "main",
            "post_condition": {
                "observed": True,
                "evidence": {
                    "session_fingerprint": FP1,
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "generation": 1,
                    "repository": "hamad933/Enterprise-Operations-Control",
                    "starting_branch": "main",
                },
            },
        },
    )


def test_lineage_index_exposes_all_durable_exact_generation_bindings():
    index = lineage_index(FakeStore(_record()), project="RP02", route="RP02")

    assert set(index) == {FP1, FP2, FP3}
    assert index[FP1] == [
        {
            "lane_id": "lane-1",
            "role": "REVIEWER",
            "workstream": "RP02-IPA-S01-001",
            "generation": 1,
            "current_candidate_sha": "5fed7c1b4648dd176907561283c3d716f159efc8",
            "current_pr_number": None,
            "identity_recovery_source": "CONFIRMED_CREATION_RECEIPT",
        }
    ]
    assert index[FP2][0]["generation"] == 2
    assert index[FP2][0]["identity_recovery_source"] == "PREVIOUS_SESSION_FINGERPRINT"
    assert index[FP3][0]["generation"] == 3
    assert index[FP3][0]["identity_recovery_source"] == "EVIDENCE_BINDINGS"
