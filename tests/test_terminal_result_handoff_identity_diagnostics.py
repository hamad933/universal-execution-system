from ues.terminal_results import _bound_result


SHA = "06d7e80af27232f416940d04dffe4a325b01e14d"
REPOSITORY = "hamad933/BOOKING-SERVICES"


def _session():
    return {
        "session_fingerprint": "f" * 64,
        "source_repository": REPOSITORY,
        "source_binding_proven": True,
    }


def _lineage():
    return {
        "role": "ASSURANCE",
        "workstream": "RP03-IPA-S01",
        "generation": 2,
        "current_candidate_sha": SHA,
    }


def _candidate(*, role="ASSURANCE", workstream="RP03-IPA-S01"):
    return {
        "structured": True,
        "role": role,
        "workstream": workstream,
        "status": "COMPLETE",
        "verdict": "PASS",
        "candidate_sha": SHA,
        "reviewed_sha": SHA,
        "context_state": "OK",
        "finding_count": 0,
        "findings": [],
        "handoff_fingerprint": "h" * 64,
    }


def _materialize(candidate):
    return _bound_result(
        project="RP03",
        route="RP03",
        repository=REPOSITORY,
        session=_session(),
        candidate=candidate,
        lineage=_lineage(),
    )


def test_matching_handoff_remains_parent_consumable_without_mismatch_diagnostics():
    result = _materialize(_candidate())
    assert result["result_state"] == "PARENT_CONSUMABLE"
    assert result["freshness_status"] == "FRESH"
    assert "handoff_identity_mismatch" not in result


def test_workstream_mismatch_is_fail_closed_and_explained():
    result = _materialize(_candidate(workstream="RP03-IPA-S10"))
    assert result["result_state"] == "STRUCTURED_HANDOFF_UNBOUND"
    assert result["freshness_status"] == "UNBOUND"
    assert result["handoff_identity_mismatch"] == "WORKSTREAM_MISMATCH"
    assert result["handoff_claimed_workstream"] == "RP03-IPA-S10"
    assert result["handoff_expected_workstream"] == "RP03-IPA-S01"
    assert result["handoff_claimed_role"] == "ASSURANCE"
    assert result["handoff_expected_role"] == "ASSURANCE"


def test_role_mismatch_is_fail_closed_and_explained():
    result = _materialize(_candidate(role="REVIEWER"))
    assert result["result_state"] == "STRUCTURED_HANDOFF_UNBOUND"
    assert result["freshness_status"] == "UNBOUND"
    assert result["handoff_identity_mismatch"] == "ROLE_MISMATCH"
    assert result["handoff_claimed_role"] == "REVIEWER"
    assert result["handoff_expected_role"] == "ASSURANCE"


def test_combined_identity_mismatch_is_fail_closed_and_explained():
    result = _materialize(_candidate(role="REVIEWER", workstream="RP03-IPA-S12"))
    assert result["result_state"] == "STRUCTURED_HANDOFF_UNBOUND"
    assert result["freshness_status"] == "UNBOUND"
    assert result["handoff_identity_mismatch"] == "ROLE_AND_WORKSTREAM_MISMATCH"
    assert result["handoff_claimed_role"] == "REVIEWER"
    assert result["handoff_expected_role"] == "ASSURANCE"
    assert result["handoff_claimed_workstream"] == "RP03-IPA-S12"
    assert result["handoff_expected_workstream"] == "RP03-IPA-S01"
