# UES Automation Control Plane V2 — Interface Freeze R2

Status: `FROZEN R2 INTEGRATION CONTRACT — SHADOW ONLY`
Workstream: `UES-AUTO-V2`
Issue: `#9`
Integration PR: `#10`
Parent contract: `docs/AUTOMATION_CONTROL_PLANE_V2.md`

This revision is the Integration Authority contract for the second correction wave. It does not accept any Domain A–E candidate and does not authorize composition, canary, activation, merge, release, deploy, publication, or project mutation.

## 1. R1 heads under review

- A: `1b3243ac2fcba70b1560656ea3e265489b3c0ca2`
- B: `d27906bd160c1c413344ef9c79978c8e8b7090aa`
- C: `145a6471cee5d78df3748dff43f6084638d3644a`
- D: `d280fcbac186d11e2d36475e15f4671807872af3`
- E: `6a91eab63ed68dd3bf853bc5d1c3f45faf8a2152`

All R2 work continues on the same isolated Domain branches. No replacement branches/domains.

## 2. Canonical lane identity

A portfolio lane is identified by the immutable tuple:

`(project, route, workstream)`

`workstream` alone is never a durable/runtime key.

The composed core will expose one canonical lane-identity helper owned by Integration Authority. When a scalar key is required, it must be deterministically derived from the complete tuple; Domain implementations must not invent competing encodings.

State, leases, operation effects, watchdog events, metrics and reconciliation must all retain the complete lane identity.

## 3. One lane, multiple role-specific actor bindings

A workstream lane can simultaneously have Writer and Reviewer actors. A single generic provider/session field is insufficient.

Canonical semantic model:

`ActorBinding(role, provider, session_id, task_id, lineage, source_repository, source_identity, proof_status, evidence_id)`

At minimum the model must support `WRITER` and `REVIEWER` independently. Provider/session reuse across incompatible lane/role bindings fails closed.

`PROVEN_EXPLICIT` is required for any provider-directed external effect. A unique heuristic match remains `PROPOSED_UNVERIFIED`.

## 4. Semantic action vs authority vs external effect

Three concepts are separate:

1. lifecycle semantic transition;
2. project authority/policy decision;
3. durable external-effect authorization/idempotency.

Action capability classes are:

- `READ_ONLY` — authoritative reads/reconciliation only;
- `CONTROL_SIGNAL` — internal escalation/Parent handoff; no provider mutation;
- `EXTERNAL_EFFECT` — provider/GitHub mutation that requires project policy, exact actor/binding evidence, durable idempotency/lease and activation authority.

`REQUEST_PARENT_REVIEW` is a `CONTROL_SIGNAL`, not a provider mutation and must not require Jules source/session proof.

No lifecycle class grants `AUTO_SAFE` by itself.

## 5. Project action policy is mandatory for every AUTO_SAFE external effect

Project adapters own an explicit action allowlist/policy. This applies to all external-effect routes, including:

- waiting-answer continuation;
- same-session failure recovery;
- reviewer findings -> Writer correction packet;
- re-review dispatch to an existing Reviewer;
- any future external effect.

Technical resolvability + clean evidence is insufficient without project authorization.

Missing action policy => fail closed. New task/session creation remains Parent-only even when budget capacity is proven.

## 6. Structured waiting Activity classifier

The shared core must expose one deterministic classifier for the latest relevant provider Activity/question.

Input is structured evidence (provider state, Activity identity/type/origin, prompt/question metadata made available to the controller, project rules/evidence), not raw keyword shortcuts.

Output includes:

- waiting class;
- classification evidence/reason;
- confidence/proof state;
- `keyword_shortcut_used = false`;
- authority-neutral result.

Unknown or insufficient evidence => `UNCLASSIFIED` and fail closed.

Words such as `db`, `database`, `architecture`, etc. alone never establish scope/shared-contract/Owner authority.

## 7. Evidence profiles

Project-specific acceptance evidence is represented generically, not by hard-coding GS/CEP checks into lifecycle.

Adapter supplies a `RequiredEvidenceProfile` / equivalent with named requirements and exact evidence bindings. Examples can include:

- required CI/check/workflow;
- workflow run attempt/artifact lineage;
- browser route/profile execution;
- other project-specific validation.

Reconciliation may only advance a gated transition when every required item for that transition is proven current. Missing required evidence => `EVIDENCE_INCOMPLETE / NOT_A_PASS`.

## 8. Drift reconciliation

Reconciliation compares previous vs current authoritative binding for at least:

- repository/source;
- branch;
- base ref/baseline;
- exact head SHA;
- authorized/write scope identity supplied by adapter;
- required evidence-profile identity.

Head SHA movement invalidates exact-SHA evidence and routes to re-CI/re-review.

Base/repository/scope/evidence-profile drift is not silently normalized; it emits a drift issue/Stop Gate requiring reconciliation before external action.

## 9. CI and artifact evidence

B R1 required-CI semantics remain authoritative for the provider layer:

- unscoped green checks never authorize PASS;
- required CI identity must be adapter-supplied;
- missing required CI => `REQUIRED_CI_MISSING / NOT_A_PASS`;
- run-attempt/artifact lineage must be proven or remain `UNPROVEN`;
- `UNKNOWN != PASS` and `NO EVIDENCE != PASS`.

A canonical binding must be able to carry the provider evidence without weakening it.

## 10. Failure cascade collapse

The shared failure layer must expose one deterministic read-only grouping function for cascaded/shared-root incidents.

It groups failures only when direct structured evidence proves a common root/evidence identity; otherwise they remain separate.

Output identifies shared blockers + affected lanes and must not create correction tasks or consume task budget.

One shared blocker must not generate duplicate correction packets/tasks for every downstream symptom.

## 11. Canonical external-effect identity

D owns durable effect identity/idempotency semantics. C emits semantic effect inputs only.

External-effect identity must include the canonical lane identity plus action and exact target bindings. Payload text/digests are request evidence, not effect identity.

Same effect + different payload => collision/fail closed, not a second effect.

Role-specific targets (Writer/Reviewer session/activity/ref/SHA) must be explicit.

## 12. Durable state contract

StateStore APIs must key workstream state by canonical lane identity/scalar lane ID derived from `(project, route, workstream)`, never bare `workstream`.

State must preserve role-specific actor bindings/proof, activation state, authority provenance, evidence bindings, lease, effect identity, request digest and operation receipt.

The deterministic local-file backend remains test/replay-only.

A concrete cross-run production backend is NOT selected by R2. Therefore real canary/ACTIVE_AUTO_SAFE remains blocked. SHADOW composition/testing may proceed later if the corrected core is clean.

## 13. Canary and ACTIVE_AUTO_SAFE

`CANARY` mode alone grants no mutation. A canary grant binds exact lane, action, target, authority event, expiry, expected start and bounded effect count.

`ACTIVE_AUTO_SAFE` still requires exact project action policy + binding/evidence proof + durable effect authorization.

No activation mode grants merge/release/deploy/publication/force-push/test weakening/acceptance.

## 14. Control-cycle health

A valid Parent/Owner Stop Gate is not a failed AUTO_SAFE execution.

However, a lane with neither a legal next action nor a valid Stop Gate is `FORGOTTEN_LANE` and makes the control cycle unhealthy/failed.

A proven AUTO_SAFE incident left untreated also makes the cycle fail.

Blocked lanes never freeze independent executable lanes.

## 15. Production-backed replay contract

Domain E must bind to the exact R2 production interfaces, not search for multiple guessed aliases once R2 functions are frozen.

Integration mode:

`fixture -> exact production API -> normalized actual -> expected`

must fail on missing binding, execution error or mismatch. No ReferenceOracle fallback.

E must exercise at least the existing 41 sanitized scenarios plus the R2 interface cases: role-specific Writer+Reviewer actors, project-wide action policy gating, forgotten-lane cycle failure, base/scope/profile drift, and failure-cascade collapse.

## 16. R2 domain ownership

### A — lifecycle/reconciliation
Owns: role-specific actor binding model; capability class (`READ_ONLY/CONTROL_SIGNAL/EXTERNAL_EFFECT`); canonical lane semantics; generic evidence profile binding; base/head/scope/profile drift detection.

### B — providers/failures
Owns: current Jules/GitHub provider behavior and evidence reads; add deterministic shared-root/cascade failure grouping. Do not weaken R1 provider safety.

### C — routing/watchdog/policy
Owns: project action allowlist across every AUTO_SAFE external-effect route; structured waiting Activity classifier; forgotten-lane cycle health; semantic effect inputs only.

### D — state/idempotency
Owns: canonical-lane keyed runtime state/effect identity; role-specific binding persistence; existing canary/idempotency/restart safety.

### E — replay
Owns tests only; align adapters to exact corrected A-D R2 APIs and expand R2 fixtures.

### Integration Authority
Owns the eventual shared identity helper/module, packaging (`ues.providers`), `ues/control_loop.py`, composition, repository-wide validation, Draft PR integration and activation adjudication.

## 17. R2 acceptance sequence

1. same-branch A–E R2 corrections;
2. direct Primary Re-review of exact five heads;
3. only if clean: compose on PR #10 integration branch;
4. fix package discovery and implement `ues/control_loop.py` on the composed candidate;
5. run repository-wide tests + `UES_CONTROL_PLANE_INTEGRATION=1` against one exact composed SHA;
6. only after clean composed evidence: GS/CEP SHADOW adapters;
7. canary remains blocked until a production cross-run StateStore backend and explicit canary authority are accepted.

`PUSH != ACCEPTANCE != COMPOSITION != MERGE != ACTIVATION`

STOP: `R2_INTERFACE_FROZEN__A_E_R2_CORRECTIONS_REQUIRED__NO_COMPOSITION_NO_CANARY_NO_ACTIVATION`