# UES Automation Control Plane V2 — Minimum Public API Freeze R2

Status: `FROZEN R2 API SUPPLEMENT — SHADOW ONLY`
Parent interface: `docs/AUTOMATION_CONTROL_PLANE_V2_INTERFACE_R2.md`

This supplement freezes the minimum public names/shapes parallel Domains must converge on. Additional private helpers are allowed, but Domain E and the later Integration Authority must not guess aliases for these contracts.

## A — lifecycle / reconciliation

Required public symbols:

- `ActionCapability` enum with exactly `READ_ONLY`, `CONTROL_SIGNAL`, `EXTERNAL_EFFECT`.
- `ActorBinding` with at least: `role`, `provider`, `session_id`, `task_id`, `lineage`, `source_repository`, `source_identity`, `proof_status`, `evidence_id`.
- `RequiredEvidenceProfile` with stable `profile_id` plus named requirements/evidence status sufficient to prove completeness/currentness.
- `canonical_lane_key(binding) -> tuple[str, str, str]` representing `(project, route, workstream)`.
- `resolve_actor_binding(binding, role)` returning the role-specific actor binding or a fail-closed missing/ambiguous result.
- `reconcile_workstream(binding, previous=None)`.
- `reconcile_portfolio(bindings, previous_by_lane=None)`.

`WorkstreamBinding` must carry multiple role-specific actor bindings, adapter-supplied write/scope identity, and required-evidence profile state.

`LifecycleResolution` must expose semantic action + `required_capability`; `REQUEST_PARENT_REVIEW` resolves as `CONTROL_SIGNAL`.

## B — providers / failures

Preserve existing R1 public provider APIs:

- `JulesClient.get_session_source_binding(...)`
- `JulesClient.send_message(...)`
- `GitHubClient.get_required_ci_evidence(...)`
- `GitHubClient.get_workflow_binding(...)`

Add exactly:

`collapse_failure_cascade(failures) -> dict`

Required output fields:

- `shared_blockers`: list;
- `affected_lanes`: mapping/list sufficient to preserve lane membership;
- `unshared_failures`: list;
- `correction_task_count`: integer, always `0` in this read-only classifier;
- `duplicate_corrections`: boolean, always `False` for the classifier output.

A shared blocker may be formed only from explicit structured common-root/evidence identity. Similar text alone is insufficient.

## C — routing / watchdog / policy

Add exactly:

`classify_waiting_activity(activity, *, provider_state, classifier_rules) -> dict`

Output fields:

- `waiting_class`;
- `classification_evidence`;
- `confidence`;
- `keyword_shortcut_used` (must be `False`);
- `authority` set only to an authority-neutral marker such as `UNDECIDED`/`POLICY_REQUIRED`.

Unknown/insufficient evidence => `waiting_class=UNCLASSIFIED`.

Existing routing functions remain public but every external-effect-capable route must accept explicit project action authorization through a single parameter named:

`project_auto_safe_actions`

This applies to:

- `route_waiting(...)`
- `route_terminal_session_failure(...)`
- `route_reviewer_to_writer(...)`
- `route_writer_to_reviewer(...)`

The action set contains stable semantic effect names. Missing set => fail closed for AUTO_SAFE external effects.

`evaluate_control_cycle(...)` must return `CONTROL_CYCLE_FAILED` for either:

- proven untreated AUTO_SAFE incident; or
- `FORGOTTEN_LANE`.

A valid Parent/Owner Stop Gate alone does not fail the cycle.

## D — state / idempotency

Public runtime state must use a scalar `lane_id` supplied from the Integration-owned canonical lane helper and must also preserve audit fields `project`, `route`, `workstream_id`.

Required shape changes:

- `WorkstreamRuntimeRecord.lane_id` is mandatory.
- StateStore workstream/lease methods take `lane_id`, not bare `workstream_id`.
- `EffectIdentity` carries `lane_id`, `project`, `route`, `workstream_id`, `action`, `target`.
- effect operation keys are derived from the complete effect identity.
- role-specific actor bindings/proof are persisted in the runtime record in sanitized form.

Preserve existing public safety semantics/functions for canary grants, `claim_operation`, unknown-write reconciliation and payload-vs-effect idempotency.

## E — replay

Production replay adapters must call the exact R2 names above. No alias search for R2 contracts.

Required integration behavior remains:

`fixture -> exact production API -> normalized actual -> expected`

Missing exact API => `BINDING_UNAVAILABLE` hard failure.

E must update all pre-R1 call signatures and add R2 scenarios for:

- simultaneous role-specific Writer + Reviewer bindings;
- project action policy on correction/re-review/session recovery;
- forgotten lane => failed control cycle;
- base/scope/evidence-profile drift;
- `collapse_failure_cascade`;
- generic required evidence profile (including browser/route-profile example).

## Integration-owned follow-up

After A–E R2 pass Primary Re-review, Integration Authority may create a shared identity module/helper implementing scalar `lane_id`, fix package discovery, compose A–E, and implement `ues/control_loop.py`.

No Domain independently creates competing public identity or authority modules.

`PUSH != ACCEPTANCE != COMPOSITION != MERGE != ACTIVATION`

STOP: `R2_API_NAMES_FROZEN__SAME_BRANCH_CORRECTIONS_REQUIRED__NO_COMPOSITION_NO_CANARY_NO_ACTIVATION`