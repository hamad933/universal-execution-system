# UES-AUTO-V2 — GS SHADOW Adapter

Status: `SHADOW BUILD CANDIDATE / NO PRODUCT MUTATION / NO ACTIVATION`

This adapter binds the shared UES control-plane semantics to GS without copying lifecycle, provider, reconciliation, routing, watchdog, state or idempotency logic into project code.

## Identity

- project: `GS`
- route: `GS`
- repository: `hamad933/GS-2`
- Jules hard ceiling represented by the adapter: `30`

The canonical lane remains the shared Integration-owned `(project, route, workstream)` identity.

## Authority boundary

The adapter defaults to an empty `project_auto_safe_actions` allowlist. That is deliberate: absence of a fresh governed project action policy must fail closed. A future caller may inject a bounded allowlist only after reading current GS authority; the adapter itself does not manufacture that authority.

New Jules task creation remains Parent-only. If lifetime consumption is not proven, `task_budget_snapshot()` returns an unknown/fail-closed budget even when current enumeration is small.

## Evidence profile

`build_evidence_profile()` maps GS evidence into generic UES requirements:

- exact-head required CI;
- exact-SHA review;
- clean reviewer contract / no disqualifying reviewer mutation;
- optional required browser profile when the lane requires one.

Evidence values and IDs are runtime inputs. The adapter does not hard-code mutable run IDs, SHAs, sessions or artifacts.

## Routing

The adapter delegates to the shared routing functions for:

- waiting continuation;
- reviewer findings -> same Writer correction;
- candidate SHA movement -> re-review;
- terminal session failure.

With no fresh project external-effect allowlist, technically routable effects escalate to Parent rather than execute. Reviewer mutation remains quarantined by the shared router.

## SHADOW cycle

`run_gs_shadow_cycle()` validates project/route/repository identity and invokes Integration-owned `run_shadow_cycle()`. It never grants mutation authority, creates tasks/sessions, or changes GS.

No merge, GS product mutation, Jules mutation, canary, activation, release or deploy is authorized by this adapter candidate.
