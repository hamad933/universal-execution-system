# UES Automation Control Plane V2 — Canary-Ready Waiting-Answer Path

Status: `IMPLEMENTATION CANDIDATE / NOT ACTIVATED / NO LIVE CANARY`

## Scope

The first canary-ready orchestration path is deliberately narrow: answer one structured waiting question on one **already-existing Jules Writer session**. It does not create a Jules task/session, create a branch/PR, dispatch a reviewer, mutate a product repository, or infer broad automation authority.

The implementation entry point is `execute_waiting_answer_canary()`.

## Required independent authority boundaries

Before a provider call the orchestration requires all of the following:

1. canonical lane ID exactly equal to `canonical_lane_id(project, route, workstream)`;
2. explicit project action policy containing the semantic action `WAITING_SAME_SESSION_CONTINUATION` — the raw effect label `waiting-answer` is **not** project authority;
3. a non-empty `project_policy_evidence_id` identifying the governed policy source used for that decision;
4. durable runtime state for the exact lane;
5. exact project/route/workstream identity match;
6. role-specific `WRITER` binding;
7. provider = Jules;
8. exact existing Writer session match;
9. explicit proof status;
10. exact source repository match;
11. exact source identity match;
12. current structured observation proving `provider_state=AWAITING_USER_FEEDBACK` and the exact waiting activity;
13. runtime activation mode accepted by Domain-D state safety;
14. matching unexpired one-shot `CanaryGrant` bound to the exact effect and authority event;
15. that grant itself must bind its `expected_start` to `AWAITING_USER_FEEDBACK` and the exact waiting activity;
16. successful idempotency check;
17. lane-local lease;
18. durable operation `IN_FLIGHT` record before provider mutation.

These are separate gates. Runtime `CANARY` mode, semantic lifecycle classification, adapter configuration or CI success alone grants none of them. The project-policy evidence ID and canary authority-event ID are sanitized into the durable operation receipt before the provider call. The canary grant is consumed while the caller owns the durable lane lease and before the provider call.

Current GS and CEP adapters intentionally contain `project_auto_safe_actions=[]`; therefore the live canary path remains unreachable for both projects without a separate governed project-policy change.

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

- documented mutation-capable state;
- expected repository;
- explicit Jules source proof;
- source/repository match;
- expected source match;
- Activities read before POST;
- `{"prompt": ...}` payload;
- authoritative Activities readback after POST.

The reviewed Domain-B safety correction is required: even when the POST returns HTTP success, failure of the authoritative post-send session/Activities readback becomes `WriteOutcomeUnknown` with `safe_to_blind_retry=false`. It must never be simplified to a normal read failure or trigger a resend.

The UES API secret remains runtime-only and is not accepted by or persisted in the orchestrator.

## Ambiguous and abnormal outcomes

No ambiguous provider outcome is retried blindly. `WriteOutcomeUnknown`, provider errors after durable claim, malformed provider receipts and unexpected provider-boundary exceptions are conservatively persisted as `UNKNOWN` and stop at authoritative reconciliation. Unexpected exception text is not persisted.

If persistence of the UNKNOWN state itself fails, the result stops at `STATE_RECONCILIATION_REQUIRED`; the caller must not infer that no provider effect occurred.

`reconcile_waiting_answer_operation()` can later record an authoritative post-state observation. It performs no provider write and cannot retry the original effect.

## Success

A provider receipt is accepted as confirmed only when it reports a supported delivered outcome, contains authoritative activity evidence, and declares `safe_to_blind_retry=false`. UES sanitizes the receipt, records authoritative readback as observed, marks the operation `CONFIRMED`, and releases the lane lease.

If Jules has already authoritatively confirmed delivery but durable UES confirmation persistence then fails, the result is a state-reconciliation gate rather than a second provider call.

An identical replay is idempotently suppressed. A changed prompt for the same session/activity effect is rejected as an operation-ID collision.

## Test boundary

Repository tests use deterministic local StateStore and fake Jules clients only. They perform no network request, create no live state ref, use no API key, and send no real provider message. The full repository suite also retains the Domain-B provider regressions for HTTP-success/post-readback failure.

The tests cover policy denial, raw-effect-label authority confusion, current GS/CEP empty policy, missing policy provenance, noncanonical lane identity, exact observed-start/grant-start binding, missing authority, unproven Writer binding, wrong session/source, successful one-shot confirmation, duplicate suppression, payload collision, ambiguous writes, provider errors, unexpected provider exceptions, malformed receipts and later readback reconciliation.

## Authority and remaining live prerequisites

This implementation makes the shared system **canary-ready in code only** after exact-head review. It does not authorize a live canary.

A live canary still requires separately:

- accepted production cross-run StateStore deployment, not merely implementation code;
- a governed private runtime-state repository and narrowly scoped credential;
- explicit relevant project Parent authority;
- exact project action policy plus policy evidence;
- exact existing actor/session/source binding;
- exact unexpired one-shot canary grant and expected-start binding;
- independent assurance appropriate to a secret-bearing mutation path;
- current project gates and task-budget safety.

No merge, main/integration mutation, live StateStore ref, Jules message, GS/CEP product mutation, new Jules task, canary execution, activation, release or deploy is authorized by this candidate.
