# UES Fast Reaction and Learned Recovery

Status: `CANDIDATE CONTRACT — REQUIRES EXACT-HEAD CI AND CONTROLLER REVIEW`
Owner: `hamad933/universal-execution-system`
Scope: shared UES runtime behavior; project adapters retain project-specific authority.

## Objective

Reduce avoidable automation latency and repeated recovery failures by turning verified recurring states into deterministic, machine-actionable recovery rules. An hourly Parent Controller cycle remains a supervisory/reconciliation layer; it must not become the latency floor for a safe transition that UES can already detect.

## Ownership map

- `ues/recovery_catalog.py` — lifecycle observation -> next recovery transition.
- `ues/watchdog.py` — stalled/waiting/forgotten/reuse-drag detection and configurable thresholds.
- `ues/task_budget.py` — project-policy-aware task/session capacity gate.
- `adapters/<project>.json` — project authority, ceilings, thresholds, and replacement policy.
- `ues/lifecycle_runtime.py` / `ues/lineage_effects.py` — execute only the recovery actions already authorized and exact-bound.
- `tests/**` — every durable learned recovery rule must have a regression/replay case.

Do not create a parallel generic `lessons.py` source that duplicates executable recovery truth.

## Learned recovery invariants

1. `UNBOUND` is a reconciliation state, not proof that a new provider task/session is needed.
2. Unknown write outcome always routes to authoritative post-write reconciliation before any retry or replacement.
3. Terminal/context-exhausted replacement stays in the same logical lineage and requires exact replacement spec, duplicate-safety, project budget/authority, idempotency, and authoritative readback.
4. A stale completed Reviewer may start the next Reviewer generation for the current exact SHA when the project policy explicitly permits it; stale review never counts as current acceptance.
5. An unstructured completed Reviewer is adjudicated rather than blindly replaced.
6. Reuse is preferred only while valid and non-blocking. Proven non-viable reuse that delays a ready replacement is a watchdog incident.
7. Unknown lifetime provider history is not a universal deny rule. The project adapter decides whether unknown history fails closed; a directly proven hard ceiling always stops creation.
8. One blocked lane never freezes unrelated safe executable lanes.

## Reaction cadence

The shared lineage workflow should use a short periodic safety net and event-driven triggers where available. Cadence is an operational policy, not authority. Faster scheduling never widens product mutation, merge, release, deploy, or publication rights.

## Learning loop

`OBSERVE -> VERIFY -> CLASSIFY -> ENCODE RECOVERY RULE -> ADD REGRESSION -> DEPLOY UNDER PROJECT POLICY -> VERIFY POST-CONDITION -> RETAIN/REFINE`

A lesson is retained only when verified and reusable. Transient provider quirks, unverified guesses, and one-off noise are not promoted into timeless behavior.

## Safety

`FAST != UNGUARDED`

Speed improvements must preserve exact repository/session/source/SHA binding, one writer per write domain, no blind retry, no guessed ownership, no test weakening, and explicit Stop Gates for authority or evidence that UES cannot resolve.
