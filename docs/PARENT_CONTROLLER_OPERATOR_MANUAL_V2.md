# UES-AUTO-V2 — Parent Controller Operator Manual

Status: CURRENT operator contract for low-friction Parent Controller operation.

## 1. Operating goal

UES should feel like a transparent execution substrate between a Parent Controller and Jules, not a second bureaucracy layer.

The Parent Controller remains the project authority, planner, primary reviewer, adjudicator, and integration coordinator. Jules remains an execution provider behind UES. UES owns repetitive mechanics that should not be pushed onto the Owner:

- exact repository/ref/SHA binding;
- Current Authority transport;
- existing-session reuse;
- guarded initial/successor Jules generation;
- duplicate prevention;
- task-budget checks;
- pending-before-write idempotency;
- UNKNOWN reconciliation;
- authoritative provider readback;
- StateStore binding/accounting;
- lifecycle/watchdog receipts.

The Owner should normally speak in project terms such as `continue`, `review`, `fix`, `use Jules`, or `complete the authorized wave`. The Parent Controller translates governed project intent into UES mechanics internally.

## 2. Truth and authority do not move into UES

The Parent Controller ingress is transport only. It is never the truth owner and never grants project authority.

Before a live effect, reconstruct the project from the canonical portfolio bootstrap chain and fresh Project Current Authority. GitHub remains technical truth. Provider state remains provider truth. UES validates transported authority but cannot invent missing project authority.

The semantic request therefore contains a fresh `current_authority` object sourced from `DRIVE_CURRENT_STATE`. The Parent Controller constructs it from direct governed sources; the Owner should not be asked to hand-build it.

## 3. Single low-friction ingress

The canonical Parent Controller path is:

```text
fresh Project Current Authority
→ update only .ues/parent-controller-request.json on ues-parent-control
→ Validate Universal Core exact-head core PASS
→ read-only Parent Controller preflight in the same validated workflow
→ exact live UES main + semantic Current Authority validation
→ separate authority-gated execute job
→ existing lifecycle + guarded initial-lineage runtime
→ required durable sanitized receipt artifact
→ StateStore/provider/project post-readback
```

There is intentionally **one automatic Parent Controller transport path**. Legacy `pull_request_target`, manual comment wakeups, `workflow_run` relays, and cross-workflow `workflow_dispatch` handoffs are retired. `ues-control` remains a separate v1 format-fix mechanism and is not Parent Controller lifecycle ingress.

The Parent Controller performs one semantic operation:

```text
SUBMIT_AUTHORIZED_PROJECT_CYCLE
```

Internally:

1. Re-read live UES `main` and capture the full 40-hex SHA.
2. Reconstruct fresh Project Current Authority.
3. Build one `UES_PARENT_CONTROLLER_REQUEST_V1` payload.
4. Replace only `.ues/parent-controller-request.json` on `ues-parent-control`.
5. Do not post a manual comment, do not invoke a workflow dispatch, and do not call Jules directly.
6. Exact-head `Validate Universal Core` verifies the control head.
7. After `core` succeeds, the read-only Parent Controller preflight independently verifies the persistent PR/head/Owner/path, resolves live UES `main`, checks semantic authority, and suppresses already-receipted requests.
8. Only after that preflight does a separate execute job receive `contents:write` and `JULES_API_KEY`.
9. The execute job re-reads UES `main` immediately before effects, then reuses the existing governed lifecycle and guarded initial-lineage runtimes.
10. Consume the required result artifact containing the sanitized receipt plus compact runtime results, then verify StateStore, provider, project GitHub/CI/artifacts and continue project adjudication. A PR comment is only a best-effort UI projection and is never the canonical execution receipt.

Do not ask the Owner to open GitHub Actions, paste `current_authority_json`, manage Jules IDs, or trigger a second workflow.

## 4. Request contract

The transport request is intentionally small:

```json
{
  "schema_version": "UES_PARENT_CONTROLLER_REQUEST_V1",
  "request_id": "RP02-IPA-CYCLE-<fresh-id>",
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
    "generation_policy": {
      "authorized_initial_lineages": {},
      "authorized_lineages": {}
    }
  },
  "wakeup": {
    "event_type": "EXTERNAL_RECONCILIATION_REQUEST",
    "event_id": "<fresh-event-id>",
    "repository": "<project-repository>",
    "workstream": "<optional-current-workstream>",
    "sha": "<optional-exact-project-sha>"
  }
}
```

The example is structural only. Never copy stale project state, IDs, branches, workstreams, or SHAs from documentation. Reconstruct them directly for each cycle.

`wakeup` may be omitted when no extra routing metadata is useful; UES supplies the safe default `EXTERNAL_RECONCILIATION_REQUEST` and uses `request_id` as the event ID.

### Public-Git transport boundary

`ues-parent-control` is Git history in the UES repository. Persist only non-secret, non-sensitive governed control data appropriate to that repository. Never place provider keys, passwords, private keys, tokens, credentials, sensitive production data, or confidential payloads in the request.

If a future project's authority envelope contains material unsuitable for UES repository history, use an authorized private transport while preserving the same UES Current Authority/runtime gates.

## 5. Trust boundaries

### A. Exact-head core validation

`Validate Universal Core` first runs the normal exact-head validation suite. Parent Controller execution cannot start until `core` passes.

### B. Read-only Parent Controller preflight

The preflight has only read permissions and no Jules secret. It verifies:

- same repository;
- head branch exactly `ues-parent-control`;
- PR remains OPEN / DRAFT and targets the default branch;
- event sender is repository Owner;
- PR contains exactly `.ues/parent-controller-request.json` as its changed path;
- exact control head is Owner-authored and Owner-committed and changes exactly that path;
- exact live UES default-branch SHA;
- request schema and `runtime_sha` match live UES main;
- project/route/source/current/source_id/authority_event_id/expiry are coherent;
- secret-bearing request keys are absent;
- no durable receipt already confirms the same request ID + digest + runtime SHA.

The control branch is data only. The preflight reads the request file through GitHub API and checks out only the exact trusted UES `main` runtime for semantic validation.

A failed preflight writes a sanitized pre-effect failure receipt and never reaches the effect job.

### C. Authority-gated execute job

Only the separate execute job receives `contents:write` for UES StateStore and `JULES_API_KEY`. Immediately before its effect step, it reads UES `main` again. If main moved, execution fails closed; the Parent Controller must reconstruct a fresh request rather than silently use stale runtime code.

The execute job is serialized only at `ues-project-lifecycle-<project>`, so independent RP01–RP04 Parent Controllers are not globally blocked by one transport queue.

## 6. Effect boundary

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

These are the existing governed runtimes. Ingress does not reimplement or weaken their Current Authority, task-budget, duplicate, UNKNOWN, idempotency, exact-binding, or provider-readback checks.

## 7. Durable receipt and evidence

Every executed request must render `UES_PARENT_CONTROLLER_RECEIPT_V1` and include it in the required result artifact together with:

- `parent-request-metadata.json`;
- `lifecycle-result.json`;
- `initial-lineage-result.json`;
- `parent-controller-receipt.md`.

The receipt is sanitized and binds at minimum:

- request ID and digest;
- authority event ID;
- project;
- exact UES runtime SHA;
- validation run ID;
- execution outcome;
- lifecycle result;
- initial-lineage result;
- external-effects count where directly available;
- new task/session count where directly available;
- `safe_to_blind_retry=false`;
- `raw_session_ids_persisted=false`;
- `secret_material_persisted=false`.

The result artifact is the canonical durable in-workflow receipt. A PR comment may project the same receipt for convenience, but that projection is best-effort and its failure must not convert a successful UES execution into a failed Parent Controller cycle.

Receipt suppression prevents an already-receipted identical request from executing again when a matching receipt is observable through the configured receipt projection. Regardless of projection state, a missing PR comment is **not** proof that an effect failed; reconcile the result artifact, StateStore, provider, and GitHub before deciding whether any fresh request is safe.

## 8. Parent Controller task generation

When a new Jules physical generation is genuinely required, build the complete task contract from governed project intent:

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

## 9. Reuse before create

Default order:

1. reuse an existing proven Jules logical lineage/session when valid;
2. create an initial generation only when no valid lineage exists and fresh Current Authority explicitly authorizes it;
3. create a successor generation only when replacement/continuation is authorized and reuse is no longer valid.

Do not create sessions merely to exercise UES.

## 10. UNKNOWN is recovery, not retry

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

## 11. Parallel execution

Parallelize independent project lanes aggressively when authority, isolation, task budget, and write domains allow it. A blocked lane does not freeze unrelated authorized work.

Use one writer per write domain. Reviewer/assurance lanes can run broadly in parallel when candidates are frozen and independent.

Transport validation is request-local; effect serialization is project-local. RP01, RP02, RP03, and RP04 therefore do not need a repository-global Parent Controller queue lock.

## 12. Evidence and adjudication

A request commit or successful validation is not proof of a provider effect.

```text
REQUEST_COMMIT
!= VALIDATION_PASS
!= UES_EXECUTE_RECEIPT_ARTIFACT
!= PROVIDER_ACK
!= SESSION_CREATED
!= PROJECT_ACCEPTANCE
```

Use direct evidence: exact validation run/job, UES durable receipt artifact, StateStore receipt, provider readback, exact project SHA/PR/CI/artifacts, and fresh project authority. Parent Controller remains the final project adjudicator.

## 13. Persistent queue lifecycle

The dedicated branch/PR is initialized once:

```text
ues-parent-control
└── .ues/parent-controller-request.json
```

The PR stays OPEN / DRAFT / DO NOT MERGE. Each new project cycle replaces only the request file in one Owner-authored queue commit. The resulting PR validation is the complete automatic signal; no manual comment and no workflow dispatch is required.

Do not merge the persistent control PR. It is transport state, not an integration candidate.

## 14. Failure handling

If failure occurs before the authority-gated execute job:

- read the exact `Validate Universal Core` core/preflight result and available pre-effect evidence;
- do not bypass UES;
- refresh live UES main and Project Current Authority if stale;
- reconcile whether any prior receipt/effect exists;
- submit a fresh request only after prior post-state is classified.

If the execute job reaches provider/StateStore and fails or becomes UNKNOWN, use the required result artifact plus existing UES reconciliation. Never create a fresh request merely to force a retry.

A blocked project transport/effect lane must not freeze unrelated authorized projects.

## 15. UX rule

The Parent Controller hides routine UES mechanics from the Owner, not material project decisions.

Escalate only for genuine unresolved authority, product direction, material architecture/security/privacy, integration/release/deploy/publication gates, or another governed Stop Gate.

Final operating principle:

```text
AUTOMATE THE MECHANICS
PRESERVE PROJECT AUTHORITY
ONE INLINE VALIDATED PARENT-CONTROLLER PIPELINE
DURABLE ARTIFACT RECEIPT / BEST-EFFORT UI PROJECTION
NO MANUAL WAKEUP OR CROSS-WORKFLOW HANDOFF
KEEP EXACT-STATE SAFETY
REUSE BEFORE CREATE
RECOVER INSTEAD OF RETRY
PARALLELIZE SAFE LANES
MAKE UES FEEL INVISIBLE TO THE OWNER
```
