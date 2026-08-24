# UES-AUTO-V2 — Parent Controller Operator Manual

Status: candidate operator contract for frictionless Parent Controller operation.

## 1. Operating goal

UES should feel like a transparent execution substrate between a Parent Controller and Jules, not a second bureaucracy layer.

The Parent Controller remains the project authority, planner, primary reviewer, adjudicator, and integration coordinator. Jules remains an execution provider behind UES. UES owns the repetitive mechanics that should not be pushed onto the Owner:

- exact repository/ref/SHA binding;
- current-authority transport;
- existing-session reuse;
- guarded initial/successor Jules generation;
- duplicate prevention;
- task-budget checks;
- pending-before-write idempotency;
- UNKNOWN reconciliation;
- authoritative provider readback;
- StateStore binding/accounting;
- lifecycle/watchdog receipts.

The Owner should normally speak in project terms such as `continue`, `review`, `fix`, `use Jules`, or `complete the authorized wave`. The Parent Controller translates that governed project intent into UES mechanics internally.

## 2. Truth and authority do not move into UES

The control queue is transport only. It is never the truth owner and never grants project authority.

Before a live effect, the Parent Controller must reconstruct the project from the canonical portfolio bootstrap chain and fresh project Current Authority. GitHub remains technical truth. Provider state remains provider truth. UES validates the transported authority but cannot invent missing project authority.

A valid queue request therefore contains a fresh `current_authority` object sourced from `DRIVE_CURRENT_STATE`. The Parent Controller constructs it from direct governed sources; the Owner should not be asked to hand-build it.

## 3. Preferred low-friction ingress

When the ChatGPT GitHub connector cannot call `workflow_dispatch` directly, use the dedicated persistent same-repository `ues-parent-control` branch / control PR as the preferred Parent Controller ingress.

This branch is intentionally separate from legacy `ues-control`, which remains owned by the v1 format-fix queue. Separating them prevents one semantic request from waking unrelated workflows.

The Parent Controller performs one semantic operation:

```text
SUBMIT_AUTHORIZED_PROJECT_CYCLE
```

Internally that means:

1. Re-read live UES `main` and capture the full 40-hex SHA.
2. Reconstruct fresh project Current Authority.
3. Build one `UES_PARENT_CONTROLLER_REQUEST_V1` payload.
4. Create or replace `.ues/parent-controller-request.json` on `ues-parent-control`.
5. Change no other path in that queue commit.
6. Let trusted default-branch workflow code validate and execute the request.
7. Read the resulting lifecycle/provider/StateStore evidence and continue project adjudication.

Do not ask the Owner to open GitHub Actions and paste `current_authority_json`.

## 4. Request contract

The queue request is intentionally small at the transport layer:

```json
{
  "schema_version": "UES_PARENT_CONTROLLER_REQUEST_V1",
  "request_id": "RP02-IPA-CYCLE-20260824-001",
  "project": "RP02",
  "runtime_sha": "<exact-current-UES-main-40hex>",
  "current_authority": {
    "source": "DRIVE_CURRENT_STATE",
    "source_id": "<current-state-source-id>",
    "project": "RP02",
    "route": "RP02",
    "current": true,
    "authority_event_id": "<fresh-bounded-authority-event>",
    "expires_at": "<bounded-ISO8601-expiry>",
    "lineages": {},
    "generation_policy": {}
  },
  "wakeup": {
    "event_type": "EXTERNAL_RECONCILIATION_REQUEST",
    "event_id": "RP02-IPA-CYCLE-20260824-001",
    "repository": "hamad933/Enterprise-Operations-Control",
    "workstream": "<optional-current-workstream>",
    "sha": "<optional-exact-project-sha>"
  }
}
```

The example is structural only. Never copy stale project state, IDs, branches, workstreams, or SHAs from documentation. Reconstruct them directly for each cycle.

`wakeup` may be omitted when no extra routing metadata is useful; UES supplies the safe default `EXTERNAL_RECONCILIATION_REQUEST` and uses `request_id` as the wakeup event ID.

### Public-Git transport boundary

`ues-parent-control` is Git history in the UES repository. Therefore the request payload must contain only non-secret, non-sensitive governed control data that is appropriate to persist in that repository. The validator rejects common secret-bearing key names, and the Parent Controller must never place provider keys, passwords, private keys, tokens, credentials, sensitive production data, or other confidential payloads in the request.

If a future project's authority envelope contains material that is not suitable for the UES repository history, do not use this queue for that payload. Select an authorized private transport instead while preserving the same UES Current Authority/runtime gates.

## 5. What the queue validates before secrets are available

The trusted workflow fails closed unless all are true:

- event is a `pull_request_target:synchronize` from the same repository;
- head branch is exactly `ues-parent-control`;
- control PR targets the repository default branch;
- latest queue commit is authored and committed by the repository Owner identity;
- that commit changes exactly one path: `.ues/parent-controller-request.json`;
- the request file actually changed;
- request schema is closed;
- project is one of `GS`, `CEP`, `RP01`, `RP02`, `RP03`, `RP04`;
- request is bound to the exact live UES default-branch SHA;
- transported authority says `source=DRIVE_CURRENT_STATE`, `current=true`, and matches the governed project/route;
- authority has source ID, authority event ID, and bounded expiry;
- no secret-bearing keys are present in the request payload;
- wakeup type is allowlisted;
- optional repository/workstream routing metadata is restricted to safe single-line formats before it can cross a GitHub Actions job-output boundary.

The control branch is read as data through the GitHub API. It is never checked out or executed.

## 6. What happens after preflight

Only after preflight succeeds does the effect job receive `JULES_API_KEY` and `contents: write` for the UES StateStore.

For `RP01–RP04`:

```text
rp_authority_runtime
→ guarded current-authority lifecycle
→ initial_lineage_runtime
```

For `GS` / `CEP`:

```text
lifecycle_runtime_observed
→ initial_lineage_runtime
```

These are the existing governed runtimes. The queue does not reimplement or weaken their checks.

## 7. Parent Controller task generation

When a new Jules physical generation is genuinely required, build the complete task contract from governed project intent. The current runtime requires:

- objective;
- exact `branch@40hex-SHA` baseline;
- write scope;
- prohibited scope;
- validation/tests;
- evidence requirements;
- handoff;
- Stop Gate.

Writer scope must be non-empty. Reviewer/Assurance/Final Assurance initial tasks are explicitly read-only. Dynamic `provider_starting_branch` must match the exact baseline branch.

Do not expose these fields to the Owner as a questionnaire when direct project authority already determines them.

## 8. Reuse before create

Default order:

1. reuse the existing proven Jules logical lineage/session when valid;
2. create an initial generation only when no valid lineage exists and fresh Current Authority explicitly authorizes it;
3. create a successor generation only when replacement/continuation is authorized and reuse is no longer valid.

Do not create sessions merely to exercise UES.

## 9. UNKNOWN is recovery, not retry

If provider write outcome is UNKNOWN:

```text
read StateStore pending transition
→ read authoritative provider inventory
→ exact-match repository/ref/marker
→ zero match: remain UNKNOWN
→ one exact match: adopt without second create
→ multiple matches: ambiguous / adjudication required
```

Never blindly retry.

## 10. Parallel execution

Parallelize independent project lanes aggressively when authority, isolation, task budget, and write domains allow it. A blocked lane does not freeze unrelated authorized work.

Use one writer per write domain. Reviewer/assurance lanes can run broadly in parallel when candidates are frozen and independent.

## 11. Evidence and adjudication

A queue trigger is not proof of a provider effect.

```text
QUEUE_COMMIT != WORKFLOW_SUCCESS != PROVIDER_ACK != SESSION_CREATED != PROJECT_ACCEPTANCE
```

Use direct evidence: exact workflow run/job, StateStore receipt, provider readback, exact project SHA/PR/CI/artifacts, and current project authority. Parent Controller remains the final project adjudicator.

## 12. First use of the persistent queue

After this workflow is integrated to UES `main`, initialize a dedicated control branch once:

```text
ues-parent-control
└── .ues/parent-controller-request.json
```

Create a persistent Draft PR from `ues-parent-control` to `main`. Opening the PR does not execute a Parent Controller cycle because the workflow reacts only to later `synchronize` events. Each real request then replaces only `.ues/parent-controller-request.json` in one queue commit, producing a deterministic `synchronize` signal.

The legacy `ues-control` branch and `.ues/request.json` remain separate and must not be used or modified by Parent Controller lifecycle requests.

## 13. Failure handling

If the queue fails before the effect job:

- do not bypass UES;
- inspect the exact preflight failure;
- refresh live UES main / project authority if stale;
- correct only the request file;
- resubmit with a new request ID when the prior request did not execute.

If the effect job reaches provider/StateStore and fails or becomes UNKNOWN, use existing UES reconciliation. Do not create a fresh request merely to force a retry until authoritative post-state proves that is safe.

## 14. UX rule

The Parent Controller should hide routine UES mechanics from the Owner, not hide material project decisions.

Escalate only for genuine unresolved authority, product direction, material architecture/security/privacy, integration/release/deploy/publication gates, or another governed Stop Gate.

Final operating principle:

```text
AUTOMATE THE MECHANICS
PRESERVE PROJECT AUTHORITY
KEEP EXACT-STATE SAFETY
REUSE BEFORE CREATE
RECOVER INSTEAD OF RETRY
PARALLELIZE SAFE LANES
MAKE UES FEEL INVISIBLE TO THE OWNER
```
