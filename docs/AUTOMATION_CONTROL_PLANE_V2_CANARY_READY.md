# UES Automation Control Plane V2 — Canary-Ready Waiting-Answer Path

Status: `IMPLEMENTATION CANDIDATE / NOT ACTIVATED / NO LIVE CANARY`

## Scope

The first canary-ready orchestration path is deliberately narrow: answer one structured waiting question on one **already-existing Jules Writer session**. It does not create a Jules task/session, create a branch/PR, dispatch a reviewer, mutate a product repository, or infer broad automation authority.

The implementation entry point is `execute_waiting_answer_canary()`.

## Required boundaries

Before a provider call the orchestration requires all of the following:

1. canonical lane ID exactly equal to `canonical_lane_id(project, route, workstream)`;
2. an explicit project action-policy authorization supplied by the trusted caller;
3. durable runtime state for the exact lane;
4. exact project/route/workstream identity match;
5. role-specific `WRITER` binding;
6. provider = Jules;
7. exact existing Writer session match;
8. explicit proof status;
9. exact source repository match;
10. exact source identity match;
11. runtime activation mode accepted by Domain D state safety;
12. matching unexpired one-shot `CanaryGrant` bound to the exact effect and authority event;
13. exact structured observed-start state when the grant requires it;
14. successful idempotency check;
15. lane-local lease;
16. durable operation `IN_FLIGHT` record before provider mutation.

Runtime `CANARY` mode by itself grants nothing. The canary grant is consumed while the caller owns the durable lane lease and before the provider call.

## External-effect identity

The effect is the existing Domain-D waiting-answer identity:

- canonical lane;
- project / route / workstream;
- action `waiting-answer`;
- exact Jules session;
- exact waiting activity.

The answer/prompt is payload evidence, not effect identity. Therefore changing the prompt for the same effect produces an operation/request collision rather than a second send.

## Jules mutation boundary

The orchestrator calls the existing shared `JulesClient.send_message()` contract exactly once after durable claim. The client independently re-reads provider session/source state and requires:

- mutation-capable documented state;
- expected repository;
- explicit Jules source proof;
- source/repository match;
- expected source match;
- Activities read before POST;
- `{"prompt": ...}` payload;
- authoritative Activities readback after POST.

The UES API secret remains runtime-only and is not accepted by or persisted in the orchestrator.

## Ambiguous outcomes

No ambiguous provider outcome is retried blindly. If Jules cannot prove delivery, or if a provider error occurs after the durable claim, the operation becomes `UNKNOWN` and the lane stops at `AUTHORITATIVE_READBACK_REQUIRED`.

`reconcile_waiting_answer_operation()` can later record an authoritative post-state observation. It performs no provider write and cannot retry the original effect.

## Success

A provider receipt is accepted as confirmed only when it reports a supported delivered outcome, contains authoritative activity evidence, and declares `safe_to_blind_retry=false`. UES sanitizes the receipt, records authoritative readback as observed, marks the operation `CONFIRMED`, and releases the lane lease.

An identical replay is idempotently suppressed. A changed prompt for the same session/activity effect is rejected as an operation-ID collision.

## Test boundary

Repository tests use deterministic local StateStore and fake Jules clients only. They perform no network request, create no live state ref, use no API key, and send no real provider message.

## Authority

This implementation makes the system **canary-ready in code only**. It does not authorize a live canary. A live canary still requires separately accepted production cross-run state storage, explicit relevant project Parent authority, an exact one-shot canary grant, exact project action policy, exact existing actor/source binding, and all other current project gates.

No merge, main/integration mutation, live StateStore ref, Jules message, GS/CEP product mutation, new Jules task, canary execution, activation, release or deploy is authorized by this candidate.
