# UES-AUTO-V2 Canary-Ready Boundary

Status: implementation contract for review; **not live canary authority**.

## Purpose

`ues.canary_orchestration.execute_jules_waiting_canary()` is the narrow integration boundary for an eventual one-shot continuation of an **already-existing** Jules session. It composes existing UES primitives; it does not create project authority, canary authority, a Jules task/session, a production StateStore, or activation state.

## Required authority layers

A provider call is unreachable unless all of these independently hold:

1. project action policy explicitly includes `WAITING_SAME_SESSION_CONTINUATION`;
2. the effect is exactly `waiting-answer` and targets Jules plus one exact session and waiting activity;
3. an exact expected Jules repository and source are supplied;
4. StateStore contains the exact lane `(project, route, workstream)` in `CANARY` mode;
5. StateStore contains one unexpired, unconsumed `CanaryGrant` matching the exact lane/action/target and authority event;
6. any grant `expected_start` fields match the caller-supplied current observation;
7. canonical idempotency state allows the exact operation.

Runtime mode, adapter configuration, semantic lifecycle classification, or CI success alone grants none of these authorities.

Current GS and CEP adapters intentionally retain `project_auto_safe_actions=[]`; therefore this canary-ready path is **not live-reachable for GS or CEP** without a separate governed project/canary gate.

## Execution ordering

For an authorized future canary, the boundary is:

1. derive canonical effect/operation identity;
2. validate project policy and exact target;
3. claim the lane-local lease;
4. consume the one-shot canary grant;
5. persist the operation as `IN_FLIGHT`;
6. call `JulesClient.send_message()` once;
7. rely on Jules provider read-before-write source/session/Activities validation;
8. use `{"prompt": prompt}` for `sendMessage`;
9. require authoritative post-write Activities confirmation;
10. persist confirmed or ambiguous outcome;
11. never blind-retry.

No task/session creation API exists in this orchestration layer.

## Outcome handling

- Confirmed Jules receipt: persist authoritative readback with `observed=true`, transition operation to `CONFIRMED`, and clear the lane lease.
- `WriteOutcomeUnknown`: persist operation `UNKNOWN` with reconciliation required. Re-entry is blocked by idempotency and does not call Jules again.
- Definitive provider failure: persist operation `REJECTED` and release the lease. The same operation is terminal and is not auto-retried.
- Unexpected exception at the provider boundary: conservatively persist `UNKNOWN`; never infer that no external effect occurred.
- Provider confirms delivery but durable state persistence fails: report a reconciliation-required state and block further execution; do not duplicate the provider effect.

Receipts/evidence are sanitized before durable persistence. API keys and provider tokens are runtime-only.

## Production prerequisites not satisfied by this code

This code does **not** authorize or provision:

- a private production runtime-state repository;
- a credential for that repository;
- live state refs;
- a GS or CEP action allowlist;
- a live Jules session selection;
- a live canary grant;
- CANARY execution;
- ACTIVE_AUTO_SAFE;
- merge, release, deployment, or publication.

A live canary remains blocked until the governed UES/project gates explicitly provide those prerequisites and independent assurance has reviewed the exact candidate.
