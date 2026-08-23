# UES Automation Control Plane V2

Status: `DRAFT SHARED CONTROL-PLANE CONTRACT — SHADOW ONLY`
Workstream: `UES-AUTO-V2`
Issue: `#9`
Baseline: `main@a14e19d743ece010c08342e7e751eb0359dff596`
Candidate branch: `automation/portfolio-control-plane-v2`

## 1. Purpose

UES V2 provides one shared Fast Controller core for multiple portfolio projects while preserving one project-specific Parent Controller and project-specific authority. GS and CEP are the first adapters.

The core is execution/runtime infrastructure. It does not own project acceptance, merge, release, deployment, product publication, or Owner decisions.

## 2. Runtime topology

```text
Project Parent Controller (hourly supervisory reasoning)
                    |
                    v
         UES Fast Controller Core
     event-driven + light reconciliation
                    |
       +------------+------------+
       |                         |
   GS Adapter                  CEP Adapter
       |                         |
GS GitHub/Jules             CEP GitHub/Jules
```

Shared code is centralized in UES. Execution is distributed through project-local GitHub Actions and project-local secrets. Codespaces are optional development tooling only; no persistent VM/server is required.

## 3. Lifecycle invariant

Every active workstream must have a deterministic next state or an explicit authority Stop Gate.

```text
WRITER_ACTIVE
-> CANDIDATE_PUBLISHED
-> CI_RUNNING
-> CI_CLASSIFIED
-> REVIEWER_ACTIVE
-> REVIEW_RESULT
   -> PASS -> PARENT_REVIEW_PENDING
   -> FINDINGS -> CORRECTION_REQUIRED
                -> SAME_WRITER_CONTINUATION
                -> NEW_SHA
                -> PRIOR_REVIEW_STALE
                -> CI_RUNNING
                -> RE_REVIEW
```

`AWAITING_USER_FEEDBACK`, `FAILED`, `PAUSED`, CI failure, reviewer completion, and candidate SHA movement are transitions, not silent terminal no-ops.

## 4. Canonical runtime binding

For each workstream the runtime registry must be able to represent:

- project / route;
- workstream ID and role;
- Jules session/task ID when applicable;
- writer lineage and reviewer lineage;
- repository identity;
- branch;
- PR;
- exact full head SHA;
- base ref / expected baseline;
- CI run/job/artifact binding;
- review binding and reviewed SHA;
- waiting/error classification;
- action in flight / lease;
- last provider/GitHub activity;
- operation key / receipt;
- task-budget classification;
- next permitted action / Stop Gate.

Runtime state is not a replacement for Drive governed project state.

## 5. Required core modules

Target shared interfaces:

- `ues/control_loop.py` — Discover -> Normalize -> Decide -> Act -> Verify -> Continue.
- `ues/lifecycle.py` — lifecycle states/transitions and next-action resolution.
- `ues/reconciliation.py` — workstream/session/branch/PR/SHA/CI/review binding.
- `ues/providers/jules.py` — Jules Sessions/Activities/sendMessage adapter; pagination; normalized documented states; read-before-retry.
- `ues/providers/github.py` — authoritative GitHub PR/ref/CI/review/artifact reads.
- `ues/routing.py` — waiting-input, reviewer->writer, writer->reviewer routing.
- `ues/watchdog.py` — waiting/stuck/forgotten/idle-lane detection.
- `ues/state_store.py` — durable runtime state/leases/receipts abstraction.
- `ues/task_budget.py` — conservative project-specific task-budget gates.
- `ues/metrics.py` — operational metrics and sanitized dashboard receipts.

Existing UES modules such as `idempotency.py`, `operation_records.py`, `recovery.py`, `transaction.py`, `failures.py`, and `authority_transport.py` should be extended/reused rather than duplicated when their contracts fit.

## 6. Waiting-input policy

The controller must read the exact current provider state and latest relevant Activity before deciding.

Representative classes:

- `POLICY_RESOLVABLE` -> AUTO_SAFE same-session continuation when project policy permits.
- `ENVIRONMENT_MISMATCH` -> AUTO_SAFE when bounded workaround is already authorized.
- `CI_DEPENDENT` / `REVIEW_DEPENDENT` -> resolve from direct exact evidence if deterministic.
- `TOOL_LIMIT` -> bounded continuation when it does not expand scope/authority.
- `SHARED_CONTRACT_REQUIRED` -> PARENT_REQUIRED.
- `SCOPE_OR_NEW_TASK_REQUIRED` -> PARENT_REQUIRED.
- `OWNER_DECISION_REQUIRED` -> OWNER_REQUIRED.
- `UNCLASSIFIED` -> fail closed.

Same-session continuation is preferred. Waiting does not justify a new task.

## 7. Failure policy

A failed provider/CI state must be classified immediately.

- recoverable -> continue/recover same lineage;
- terminal session -> `SESSION_CONTINUATION_UNAVAILABLE` + `NEW_TASK_RECOMMENDED`;
- new-task recommendation never spends project budget automatically;
- unknown write outcome -> inspect authoritative post-state before retry;
- one blocked lane must not freeze independent lanes.

## 8. Reviewer/writer routing

Reviewer findings are routable only when:

- reviewed candidate exact SHA equals the intended candidate SHA;
- reviewer role/independence is valid;
- reviewer mutation/write activity has been adjudicated;
- writer lineage binding is proven;
- finding scope matches writer authority;
- no duplicate/in-flight correction operation exists.

Findings should be grouped by root cause and sent as one coherent packet to the existing Writer session when safe.

Any candidate SHA movement automatically makes prior exact-SHA reviews stale.

## 9. Watchdog/SLO behavior

Thresholds are project-configurable. The first implementation must support at least:

- waiting input older than threshold -> incident;
- completed review not routed -> incident;
- failed CI/provider state not classified -> incident;
- active writer/reviewer without heartbeat -> stale-heartbeat warning;
- workstream with no legal next action and no Stop Gate -> `FORGOTTEN_LANE`;
- runtime/controller cycle that leaves resolvable AUTO_SAFE incidents untreated -> cycle failure.

## 10. Activation modes

- `SHADOW` — read/reconcile/decide/emit sanitized receipts only.
- `CANARY` — one explicitly bounded mutation with read-before-write and post-readback.
- `ACTIVE_AUTO_SAFE` — only accepted AUTO_SAFE action classes.
- missing/unknown activation state -> SHADOW.

No mode grants merge/release/deploy/product publication/force-push/test weakening/Owner acceptance.

## 11. Project adapters

Project adapters/configs own only project-specific runtime configuration, for example:

- repository and route identity;
- workstream patterns;
- writer/reviewer role mappings;
- branch/PR conventions;
- allowed path scopes;
- waiting-input classifications/thresholds;
- project task-budget policy;
- secret names;
- project-specific forbidden operations.

They do not become a second source of governed current project truth.

Initial adapter targets:

- `adapters/gs.json`
- `adapters/cep.json`

## 12. Thin project workflows

GS and CEP should eventually retain only thin project-local workflows/configuration that:

1. run on trusted project context;
2. load a pinned accepted UES version/ref;
3. load project adapter/config;
4. expose project-local secrets only to the bounded provider step;
5. emit sanitized receipts/artifacts;
6. default to SHADOW;
7. never execute untrusted candidate code with control-plane secrets.

## 13. Regression corpus

The deterministic/replay suite must include observed failure classes from GS/CEP, including:

- waiting input left unresolved;
- environment mismatch waiting question;
- session/channel binding mismatch;
- failed terminal session;
- reviewer code-bearing mutation;
- PASS text without clean exact evidence;
- ambiguous writer session ownership;
- stale review after SHA movement;
- artifact/run-attempt mismatch;
- 401/403/429/5xx/network/protocol errors;
- unknown provider state;
- ambiguous write result and no-blind-retry recovery;
- duplicate correction prevention;
- task-budget lifetime uncertainty;
- one blocked lane while other lanes remain executable;
- forgotten lane / missing next transition.

## 14. Parallel writer domains

All writer tasks use this exact baseline/contract unless the integration authority publishes a new frozen interface revision.

### Domain A — lifecycle/reconciliation
Write scope: `ues/lifecycle.py`, `ues/reconciliation.py`, model additions required exclusively by those modules, targeted tests.

### Domain B — providers/recovery
Write scope: `ues/providers/**`, bounded extensions to `ues/recovery.py` / `ues/failures.py`, targeted tests.

### Domain C — routing/watchdog/policy
Write scope: `ues/routing.py`, `ues/watchdog.py`, `ues/task_budget.py`, `ues/metrics.py`, targeted tests.

### Domain D — state/idempotency/leases
Write scope: `ues/state_store.py`, bounded extensions to `ues/idempotency.py`, `ues/operation_records.py`, `ues/transaction.py`, targeted tests.

### Domain E — replay/integration test harness
Write scope: `tests/control_plane/**`, fixtures only. Must not redefine production interfaces without integration-authority approval.

### Domain F — GS adapter
Blocked until core interface freeze. Later scope: GS adapter/config + thin GS workflow only.

### Domain G — CEP adapter
Blocked until core interface freeze. Later scope: CEP adapter/config + thin CEP workflow only.

One integration authority owns `ues/control_loop.py`, cross-domain interface changes, branch integration, Draft PR metadata, and activation/adjudication.

## 15. First acceptance sequence

1. Core modules + deterministic tests on UES candidate.
2. Replay GS/CEP observed incidents without provider writes.
3. Project shadow integrations compare decisions against direct GitHub/Jules evidence.
4. One explicitly authorized same-session canary.
5. Parent-controller supervisory-cycle verification.
6. Only then consider `ACTIVE_AUTO_SAFE` for selected classes.

`PUSH != ACCEPTANCE != MERGE != ACTIVATION`.

STOP: `V2_CANDIDATE_READY_FOR_PRIMARY_REVIEW`; no merge/activation inference.
