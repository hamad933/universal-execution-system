# Universal Execution System

A lean, extensible execution-control framework for safe, fast, recoverable, and highly automated workflows across heterogeneous repositories.

## Design principles

- Small universal core; capabilities load only when a repository needs them.
- Project-family adapters plus repository-specific overrides.
- Exact-state write safety: expected SHA, operation identity, bounded authority, and post-write reconciliation.
- Structured evidence and checkpoints instead of long conversational handoffs.
- CI is a verifier and asynchronous worker, not a chat polling loop.
- Resource optimization is measured: cache/prebuild decisions are advisory and telemetry-driven.
- Repository-native commands remain authoritative; the universal layer provides semantic command names.

## Bootstrap v0

This first version is intentionally read-only. It provides:

- machine-readable protocol schemas;
- a dependency-free Python reference CLI for `status`, `detect`, `doctor`, `validate-contract`, and `resource-advice`;
- a sample project contract;
- a lean resource policy;
- repository-native validation in GitHub Actions.

Mutating bridge operations such as `format-fix`, `commit`, and `push` are deliberately deferred until exact-SHA authority and recovery behavior are validated.

## Codespaces

The repository now includes `.devcontainer/devcontainer.json` for GitHub Codespaces. It provisions only the Python 3.11 environment required by the current Universal Core and runs the unit tests after the workspace is created.

No Node, PHP, Java, browser, database, or other project-family stack is installed globally. Those capabilities belong in optional adapters or repository-specific overrides and should load only when a project needs them.

Prebuilds are intentionally off by default. They should be enabled only when measured startup time and usage frequency justify their extra storage and GitHub Actions consumption.

## Quick start

```bash
python -m ues.cli detect --repo .
python -m ues.cli status --repo .
python -m ues.cli doctor --repo .
python -m ues.cli validate-contract examples/project.contract.json
python -m unittest discover -s tests -v
```

See `docs/architecture.md` for the extension model and safety boundaries.
