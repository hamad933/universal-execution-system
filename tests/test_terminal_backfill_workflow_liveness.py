from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ues-terminal-result-backfill.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_project_lifecycle_is_in_same_matrix_lane_as_backfill() -> None:
    text = _workflow_text()
    assert "  lifecycle-readback:\n" not in text
    assert "Read this project lifecycle immediately after its backfill" in text
    assert "UES_LIFECYCLE_PROJECT: ${{ matrix.project }}" in text
    assert "group: ues-terminal-result-backfill-${{ matrix.project }}" in text


def test_backfill_failure_does_not_suppress_same_project_lifecycle_readback() -> None:
    text = _workflow_text()
    lifecycle = text.index("Read this project lifecycle immediately after its backfill")
    lifecycle_if = text.rfind("if: always()", 0, lifecycle)
    assert lifecycle_if != -1
    receipt = text.index("Preserve this project lifecycle readback receipt", lifecycle)
    receipt_if = text.rfind("if: always()", lifecycle, receipt)
    assert receipt_if != -1


def test_project_pipeline_preserves_independent_phase_timeouts() -> None:
    text = _workflow_text()
    backfill = text.index("Read-only project-scoped terminal backfill")
    lifecycle = text.index("Read this project lifecycle immediately after its backfill")
    assert "timeout-minutes: 45" in text[:backfill]
    assert "timeout-minutes: 25" in text[backfill:lifecycle]
    assert "timeout-minutes: 20" in text[lifecycle:]


def test_global_watchdog_waits_only_for_project_pipeline_completion() -> None:
    text = _workflow_text()
    watchdog = text.index("  terminal-watchdog:\n")
    watchdog_text = text[watchdog:]
    assert "needs: [discover-projects, backfill]" in watchdog_text
    assert "blocked_lane_freezes_independent_lanes" in text
    assert "fail-fast: false" in text
    assert "max-parallel: 6" in text
