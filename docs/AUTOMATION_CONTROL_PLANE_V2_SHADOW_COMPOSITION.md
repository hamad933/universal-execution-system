# UES Automation Control Plane V2 — SHADOW-Complete Composition Candidate

Status: `INTEGRATION CANDIDATE / SHADOW ONLY / NO LIVE STATE REPOSITORY / NO CANARY`

## Exact composition sources

Base R2 composition:
- PR #16 `review/ues-auto-v2-r2-composition@b611f3ffe2fb9ba9ac5849f723643a883476e2bb`

Reviewed bounded overlays:
- StateStore implementation PR #17 `525637a7f9f378be61be4548649079359e661c1b`
- GS SHADOW adapter PR #18 `ef3a930b2434f1b6590b50c3d2f7ab85ce7fc067`
- CEP SHADOW adapter PR #19 `295c6356ae5c5fc7bbdbebe69d7e3862a4fa4035`
- generic project-adapter runtime PR #20 `f7a351526e3ea495501c750383d667bfc4a004b5`

The first Integration composition commit reuses the exact source blob identities from those reviewed heads; no source PR is merged or moved.

## Composition boundary

The candidate contains:
- the R2 A–E shared core and Integration-owned canonical lane/control-loop layer;
- GitHub private-ref StateStore implementation mechanics;
- declarative GS and CEP SHADOW project configs;
- shared generic project-adapter parser/runtime;
- Integration-owned cross-project composition tests.

Project configuration cannot grant mutation authority. Both project adapters are SHADOW-only, have `mutation_allowed=false`, `runtime_mode_is_authority=false`, and an empty `project_auto_safe_actions` allowlist.

GS task budget maps the governed hard ceiling `30` and deliberately leaves reserve undefined. CEP maps ceiling `70` and reserve target `15`. Unknown lifetime Jules consumption remains fail-closed and does not authorize new task creation.

CEP structured waiting policy recognizes only normalized structured evidence for a bounded Controller-resolvable same-session question. Keyword prose is not classification evidence. Classification remains authority-neutral and cannot dispatch a provider effect while the project action allowlist is empty.

## StateStore boundary

The GitHub-ref backend is an implementation candidate for cross-run storage mechanics. This composition does not provision a private runtime-state repository, credential, seed ref, live lane state, operation state, lease, effect or receipt.

Therefore this composition does not establish live production StateStore acceptance and cannot cross a CANARY or ACTIVE_AUTO_SAFE gate.

## Validation target

Exact-head CI must prove on one full SHA:
- full repository unit suite;
- existing 48/48 production-backed replay contract;
- explicit `UES_CONTROL_PLANE_INTEGRATION=1` suite with zero environment-gated skips;
- real GS/CEP adapter configs load through the generic runtime;
- canonical GS/CEP W01 lane isolation;
- missing evidence remains NOT_A_PASS;
- structured waiting classification remains keyword-free and authority-neutral;
- control loop remains SHADOW and dispatches zero external effects/tasks;
- StateStore backend is packaged with the composed runtime.

## Prohibitions

No merge, main/integration mutation, GS/CEP product mutation, Jules/provider mutation, new Jules task, live runtime-state ref, canary, activation, release or deploy is authorized by this candidate.

`PUSH != PRIMARY_REVIEW != STATESTORE_LIVE_ACCEPTANCE != CANARY != ACTIVATION != MERGE`
