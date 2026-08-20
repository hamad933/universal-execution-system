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

## UES v1

UES v1 has a proven bounded-write path using the ChatGPT GitHub Connector:

```text
recover live state
→ bind the change to the exact current parent SHA
→ create the smallest authorized Git-object change
→ fast-forward the target ref with force=false
→ verify live HEAD, ancestry, and changed paths
→ validate the exact final SHA
→ record a durable receipt/checkpoint
```

GitHub Actions remains the preferred verifier and long-running repository-native execution backend. Local workspaces, Codespaces, self-hosted runners, and other backends are optional capabilities rather than prerequisites.

The primary operator rule is simple: live GitHub state is authoritative, and an uncertain write is never blindly retried.

## Start here

For a fresh ChatGPT execution chat, use:

- [`docs/CHAT_OPERATOR_MANUAL.md`](docs/CHAT_OPERATOR_MANUAL.md) — the complete UES v1 operating manual, including the copy-paste bootstrap prompt for any chat.
- [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md) — optional future apps/tools/infrastructure, with clear triggers for when they are worth adding.
- [`docs/architecture.md`](docs/architecture.md) — core architecture and extension model.
- [`docs/adapters.md`](docs/adapters.md) — project-family adapter model.
- [`docs/transactions.md`](docs/transactions.md) — authority, CAS, transaction, and recovery mechanics.

## Codespaces

The repository includes `.devcontainer/devcontainer.json` for GitHub Codespaces. It provisions only the Python environment required by the Universal Core and runs the unit tests after the workspace is created.

No Node, PHP, Java, browser, database, or other project-family stack is installed globally. Those capabilities belong in optional adapters or repository-specific overrides and should load only when a project needs them.

Prebuilds are intentionally off by default. They should be enabled only when measured startup time and usage frequency justify their extra storage and GitHub Actions consumption.

## Developer quick start

```bash
python -m ues.cli detect --repo .
python -m ues.cli status --repo .
python -m ues.cli doctor --repo .
python -m ues.cli validate-contract examples/project.contract.json
python -m unittest discover -s tests -v
```

For operational use, start with `docs/CHAT_OPERATOR_MANUAL.md` rather than reconstructing the protocol from previous conversations.
