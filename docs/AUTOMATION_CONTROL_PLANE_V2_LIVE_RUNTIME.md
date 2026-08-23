# UES-AUTO-V2 — Live Runtime Foundation

Status: `CANDIDATE / OWNER-AUTHORIZED SAME-REPOSITORY STATESTORE / SHADOW WATCHDOG / NO PROJECT MUTATION`

## Purpose

This layer turns the already-reviewed UES control-plane components into a live runtime foundation without widening project mutation authority.

It provides:

- an explicit owner-authorized Git-ref StateStore policy for using the UES repository itself;
- durable lane and operation refs with non-force CAS and authoritative readback;
- restart discovery for lane and operation identities;
- a live read/write/recovery/CAS/lease StateStore proof on a UES-only SHADOW probe lane;
- a read-only runtime watchdog audit over durable lanes and operations;
- a bounded one-page Jules authentication probe using `JULES_API_KEY` at runtime only;
- a scheduled read-only watchdog definition that becomes active only if this workflow reaches the default branch through a separately authorized integration gate.

## Public same-repository state policy

The normal `GitHubGitDataTransport` remains private-repository-only. Public same-repository storage is implemented by a separate explicit policy class and requires all of the following:

1. runtime repository identity exactly equals the owner-authorized repository identity;
2. `UES_ALLOW_PUBLIC_SAME_REPO_STATE=true` is explicitly set at runtime;
3. the GitHub token exists only in process memory;
4. persisted state is passed through the existing secret sanitizer;
5. ref updates remain non-force CAS with authoritative post-write readback;
6. the runtime namespace is isolated under `ues-runtime/v2`.

Public storage does **not** provide metadata confidentiality. Non-secret lane, session, source, repository and operation identifiers persisted by future callers may be visible in public refs. Raw API keys, authorization headers and secret-shaped values must never be persisted.

## Runtime credentials

- `GITHUB_TOKEN`: GitHub Actions built-in runtime credential used only for the same UES repository StateStore refs. No separate user-provided StateStore credential is required for this workflow.
- `JULES_API_KEY`: repository secret consumed only by the Jules read-only authentication probe in this candidate. It is never printed, persisted in StateStore, committed, or passed to project repositories.

Credential availability is runtime configuration, not a repository-code fact. Any creation, rotation, rename, deletion, or permission change affecting `JULES_API_KEY` requires a fresh bounded read-only probe and durable sanitized readback before Jules connectivity is treated as proven again.

## Live StateStore proof

`python -m ues.live_runtime state-smoke` operates only on the canonical UES lane:

`UES / INTERNAL:UES / LIVE-RUNTIME-PROBE`

It proves:

1. exact same-repository storage policy verification;
2. live lane write and authoritative readback;
3. backend reconstruction / runner-replacement recovery;
4. stale CAS rejection without overwrite;
5. lane-local lease persistence across backend reconstruction;
6. lease release by exact identity;
7. durable confirmed operation record;
8. restart discovery of lane and operation identities.

It performs no Jules request and no product-project mutation.

## Runtime watchdog

`python -m ues.live_runtime state-audit` discovers all runtime refs in the configured namespace and performs a read-only audit. `IN_FLIGHT` or `UNKNOWN` operations, durable unknown-write state, corrupt state, or stale leases become blocking reconciliation conditions. Independent healthy lanes remain executable; a blocked lane does not create a global portfolio freeze.

The existing UES semantic control loop and watchdog remain responsible for higher-level waiting/review/failure/evidence-drift routing when authoritative project observations are supplied. This live runtime audit adds cross-run durable-state supervision; it does not invent missing project authority or provider state.

## Jules probe

`python -m ues.live_runtime jules-probe` performs exactly one read-only request:

`GET /v1alpha/sessions?pageSize=1`

It does not follow pagination, does not emit session identities, does not call `sendMessage`, and returns only sanitized authentication/read status.

## Workflow safety

`.github/workflows/ues-live-runtime-foundation.yml` deliberately runs the secret-bearing Jules probe only on the trusted candidate branch push or explicit workflow dispatch path, never on an untrusted pull-request event. The scheduled job is StateStore-watchdog-only and does not receive the Jules secret.

`PUSH != REVIEW != LIVE PROJECT CANARY AUTHORITY != ACTIVATION != MERGE != RELEASE`

## Remaining project-canary gate

A real Jules mutation is still project-specific. Before one live `sendMessage` can occur, UES requires an exact current project action policy, exact Writer/session/source/repository binding, a one-shot grant bound to current provider state/activity, durable StateStore readiness, and the required independent assurance. Current GS/CEP empty action allowlists are not changed by this runtime foundation.
