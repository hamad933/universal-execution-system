from types import SimpleNamespace

from ues.terminal_results import lineage_index


FP1 = "1" * 64
FP2 = "2" * 64
FP3 = "3" * 64
FP4 = "4" * 64
FP5 = "5" * 64
CONFLICT = "a" * 64


class Read:
    def __init__(self, record):
        self.status = "OK" if record is not None else "MISSING"
        self.record = record


class Store:
    def __init__(self, lane, operations):
        self.lane = lane
        self.operations = operations

    def discover_lane_ids(self):
        return ["lane"]

    def read_workstream(self, lane_id):
        assert lane_id == "lane"
        return Read(self.lane)

    def discover_operation_keys(self):
        return list(self.operations)

    def read_operation(self, key):
        return Read(self.operations[key])


def _lane():
    return SimpleNamespace(
        project="RP02",
        route="RP02",
        workstream_id="LINEAGE::RP02-IPA-S01-001::REVIEWER",
        evidence_bindings={
            "role": "REVIEWER",
            "workstream": "RP02-IPA-S01-001",
            "generation": 5,
            "session_fingerprint": FP5,
            "previous_session_fingerprint": FP4,
            "current_candidate_sha": "5fed7c1b4648dd176907561283c3d716f159efc8",
        },
        operation_receipt=None,
    )


def _operation(
    fp,
    generation,
    *,
    action="create-session-generation",
    state="CONFIRMED",
    project="RP02",
    route="RP02",
    lane_id="lane",
    workstream="RP02-IPA-S01-001",
    role="REVIEWER",
    observed=True,
    creation_kind=None,
):
    workstream_id = f"LINEAGE::{workstream}::{role}"
    target = {"role": role, "generation": str(generation)}
    evidence = {"session_fingerprint": fp, "generation": generation}
    if creation_kind is not None:
        target["creation_kind"] = creation_kind
        evidence["creation_kind"] = creation_kind
    return SimpleNamespace(
        action=action,
        state=state,
        lane_id=lane_id,
        workstream_id=workstream_id,
        effect_identity={
            "lane_id": lane_id,
            "project": project,
            "route": route,
            "workstream_id": workstream_id,
            "action": action,
            "target": target,
        },
        authoritative_readback={
            "observed": observed,
            "evidence": evidence,
        },
    )


def _initial_operation(fp, *, generation=1, **kwargs):
    return _operation(
        fp,
        generation,
        action="create-initial-lineage-session",
        creation_kind="INITIAL_LOGICAL_LINEAGE",
        **kwargs,
    )


def test_recovers_generations_older_than_previous_fingerprint():
    index = lineage_index(
        Store(_lane(), {"g2": _operation(FP2, 2), "g3": _operation(FP3, 3)}),
        project="RP02",
        route="RP02",
    )

    assert index[FP2][0]["generation"] == 2
    assert index[FP2][0]["identity_recovery_source"] == "CONFIRMED_GENERATION_OPERATION"
    assert index[FP3][0]["generation"] == 3
    assert index[FP4][0]["generation"] == 4
    assert index[FP5][0]["generation"] == 5


def test_recovers_initial_generation_from_confirmed_initial_operation():
    index = lineage_index(
        Store(_lane(), {"g1": _initial_operation(FP1)}),
        project="RP02",
        route="RP02",
    )

    assert index[FP1][0]["generation"] == 1
    assert index[FP1][0]["identity_recovery_source"] == "CONFIRMED_INITIAL_LINEAGE_OPERATION"


def test_confirmed_generation_operation_supersedes_stale_previous_convenience_field():
    lane = _lane()
    lane.evidence_bindings = {
        **lane.evidence_bindings,
        "generation": 3,
        "session_fingerprint": FP3,
        "previous_session_fingerprint": FP3,
    }
    index = lineage_index(
        Store(lane, {"g2": _operation(FP2, 2), "g3": _operation(FP3, 3)}),
        project="RP02",
        route="RP02",
    )

    assert index[FP2][0]["generation"] == 2
    assert index[FP2][0]["identity_recovery_source"] == "CONFIRMED_GENERATION_OPERATION"
    assert index[FP3][0]["generation"] == 3


def test_conflicting_same_generation_operation_proof_fails_closed():
    index = lineage_index(
        Store(
            _lane(),
            {"first": _operation(FP2, 2), "conflict": _operation(CONFLICT, 2)},
        ),
        project="RP02",
        route="RP02",
    )

    assert FP2 not in index
    assert CONFLICT not in index


def test_unrelated_unconfirmed_or_unobserved_operations_are_rejected():
    index = lineage_index(
        Store(
            _lane(),
            {
                "project": _operation(FP1, 1, project="RP01"),
                "route": _operation(FP2, 2, route="RP01"),
                "lane": _operation(FP3, 3, lane_id="other"),
                "state": _operation("6" * 64, 1, state="UNKNOWN"),
                "readback": _operation("7" * 64, 1, observed=False),
                "initial-state": _initial_operation("8" * 64, state="UNKNOWN"),
                "initial-readback": _initial_operation("9" * 64, observed=False),
            },
        ),
        project="RP02",
        route="RP02",
    )

    for fingerprint in (FP1, FP2, FP3, "6" * 64, "7" * 64, "8" * 64, "9" * 64):
        assert fingerprint not in index


def test_malformed_initial_generation_or_creation_kind_is_rejected():
    wrong_generation = _initial_operation("b" * 64, generation=2)
    wrong_kind = _operation(
        "c" * 64,
        1,
        action="create-initial-lineage-session",
        creation_kind="REPLACEMENT",
    )
    index = lineage_index(
        Store(_lane(), {"wrong-generation": wrong_generation, "wrong-kind": wrong_kind}),
        project="RP02",
        route="RP02",
    )

    assert "b" * 64 not in index
    assert "c" * 64 not in index


def test_store_without_operation_discovery_preserves_existing_sources():
    class LegacyStore:
        def discover_lane_ids(self):
            return ["lane"]

        def read_workstream(self, lane_id):
            assert lane_id == "lane"
            return Read(_lane())

    index = lineage_index(LegacyStore(), project="RP02", route="RP02")

    assert index[FP4][0]["generation"] == 4
    assert index[FP5][0]["generation"] == 5
