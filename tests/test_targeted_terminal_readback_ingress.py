from pathlib import Path


WORKFLOW = Path(".github/workflows/ues-targeted-project-terminal-readback.yml")


def test_owner_readback_accepts_only_canonical_and_explicit_exact_alias() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.issue.number == 191" in text
    assert "github.actor == 'hamad933'" in text
    assert "ues-terminal-readback|ues-exact-terminal-readback" in text
    assert "(?: ([A-Za-z0-9][A-Za-z0-9._:-]{0,127}))?" in text
    assert "exact owner readback command required" in text


def test_alias_does_not_change_readback_safety_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "python -m ues.exact_terminal_readback" in text
    assert '"provider_mutation_performed": False' in text
    assert '"new_tasks_or_sessions_created": 0' in text
    assert '"safe_to_blind_retry": False' in text
    assert "cancel-in-progress: false" in text
