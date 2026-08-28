from ues.lineage_registry import match_lineage_session


def _session(*, fp: str, repo: str = "hamad933/example", branch: str = "main", state: str = "COMPLETED"):
    return {
        "_session_fingerprint": fp,
        "_source_repository": repo,
        "sourceStartingBranch": branch,
        "normalizedState": state,
        "updateTime": "2026-08-25T00:00:00Z",
    }


def test_exact_fingerprint_and_repo_survive_missing_provider_branch_metadata():
    result = match_lineage_session(
        [_session(fp="a" * 64, branch="")],
        {
            "known_session_fingerprints": ["a" * 64],
            "provider_starting_branch": "feature/candidate",
        },
        repository="hamad933/example",
    )

    assert result["status"] == "PROVEN"
    assert result["reason"] == "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_METADATA_UNAVAILABLE"
    assert result["provider_starting_branch_metadata_missing"] is True
    assert result["provider_starting_branch_metadata_drift"] is False
    assert result["expected_provider_starting_branch"] == "feature/candidate"
    assert result["observed_provider_starting_branch"] is None


def test_exact_fingerprint_and_repo_survive_explicit_provider_branch_drift():
    result = match_lineage_session(
        [_session(fp="a" * 64, branch="main")],
        {
            "known_session_fingerprints": ["a" * 64],
            "provider_starting_branch": "feature/candidate",
        },
        repository="hamad933/example",
    )

    assert result["status"] == "PROVEN"
    assert result["reason"] == "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_DRIFT"
    assert result["provider_starting_branch_metadata_missing"] is False
    assert result["provider_starting_branch_metadata_drift"] is True
    assert result["expected_provider_starting_branch"] == "feature/candidate"
    assert result["observed_provider_starting_branch"] == "main"


def test_wrong_fingerprint_remains_unbound_even_when_repo_and_branch_match():
    result = match_lineage_session(
        [_session(fp="b" * 64, branch="feature/candidate")],
        {
            "known_session_fingerprints": ["a" * 64],
            "provider_starting_branch": "feature/candidate",
        },
        repository="hamad933/example",
    )

    assert result["status"] == "UNBOUND"
    assert result["reason"] == "NO_EXACT_GOVERNED_SESSION_FINGERPRINT_MATCH"


def test_wrong_repository_remains_unbound_even_with_exact_fingerprint():
    result = match_lineage_session(
        [_session(fp="a" * 64, repo="hamad933/other")],
        {
            "known_session_fingerprints": ["a" * 64],
            "provider_starting_branch": "main",
        },
        repository="hamad933/example",
    )

    assert result["status"] == "UNBOUND"
    assert result["reason"] == "NO_EXACT_GOVERNED_SESSION_FINGERPRINT_MATCH"


def test_matching_branch_reports_no_drift_or_missing_metadata():
    result = match_lineage_session(
        [_session(fp="a" * 64, branch="main")],
        {
            "known_session_fingerprints": ["a" * 64],
            "provider_starting_branch": "main",
        },
        repository="hamad933/example",
    )

    assert result["status"] == "PROVEN"
    assert result["reason"] == "EXACT_GOVERNED_LINEAGE_BINDING"
    assert result["provider_starting_branch_metadata_missing"] is False
    assert result["provider_starting_branch_metadata_drift"] is False
