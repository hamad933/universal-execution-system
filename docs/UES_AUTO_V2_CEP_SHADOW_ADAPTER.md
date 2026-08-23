# UES-AUTO-V2 — CEP SHADOW Adapter

Status: `SHADOW BUILD CANDIDATE / NO PRODUCT MUTATION / NO ACTIVATION`

This adapter binds shared UES control-plane semantics to CEP. It deliberately does not copy CEP AUTO-001 implementation behavior into the shared core.

## Identity

- project: `CEP`
- route: `PERSONAL:CEP`
- repository: `hamad933/Cybersecurity-Education-Platform`
- Jules ceiling: `70`
- reserve target: `15`

The canonical lane remains the shared `(project, route, workstream)` identity.

## Authority boundary

The default project action allowlist is empty. Current CEP authority requires SHADOW/fail-closed behavior for the autonomous controller and does not grant broad provider mutation. Future external-effect policy must be injected from fresh governed project authority; the adapter does not infer it from runtime activation fields, CI success or technical resolvability.

New Jules task creation remains Parent-only. Lifetime task consumption uncertainty keeps the task-budget result fail-closed.

## Structured waiting classification

The adapter supplies one normalized structured rule for the bounded class of an existing same-session Controller-resolvable question:

- provider state `AWAITING_USER_FEEDBACK`;
- normalized `question_scope = CONTROLLER_RESOLVABLE`;
- normalized `continuation_scope = SAME_SESSION`;
- `scope_expansion = false`.

Provider prose/keywords do not participate in the match. The classification result remains authority-neutral (`POLICY_REQUIRED`). Routing still needs project action policy and exact continuation evidence.

This preserves the lesson from the bounded W04 continuation without hard-coding a Jules session, PR, SHA, prompt text or one-off incident ID.

## Evidence profile

`build_evidence_profile()` maps CEP project evidence into generic UES requirements:

- Core CI;
- Release and Browser Verification;
- exact-SHA review;
- optional route-specific browser evidence;
- optional architecture-contract evidence.

The last two are explicit so a generic browser green result or unrelated assurance cannot silently satisfy a route/architecture-specific gate.

## SHADOW cycle

`run_cep_shadow_cycle()` validates CEP identity and delegates to Integration-owned `run_shadow_cycle()`. It never sends Jules messages, creates tasks/sessions, mutates GitHub, or changes CEP activation state.

No merge, CEP product mutation, live Jules mutation, canary, activation, release or deploy is authorized by this adapter candidate.
