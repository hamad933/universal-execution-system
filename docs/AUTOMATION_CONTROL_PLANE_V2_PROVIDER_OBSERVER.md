# UES-AUTO-V2 — Live Provider Observer

Status: candidate read-only runtime extension.

## Purpose

The default-branch runtime previously audited only UES durable StateStore. It did not itself populate fresh GS/CEP Jules provider observations, so Parent Controllers could encounter `PROVIDER_STATE_UNAVAILABLE` even while Jules sessions existed.

This observer closes that integration gap without granting provider mutation authority.

## Read path

Every scheduled observation performs only Jules GET operations:

1. list Jules sources;
2. list Jules sessions;
3. associate a session to GS or CEP only when its Jules source resource contains the exact project repository identity;
4. list Activities for sessions belonging to those exact repositories;
5. persist a metadata-minimized observation to the UES same-repository StateStore.

No `sendMessage`, task/session creation, plan approval, repository mutation, or product mutation is performed.

## Privacy boundary

The UES StateStore currently uses public refs in the same UES repository. Therefore the provider observer intentionally does **not** persist:

- raw Jules session IDs;
- raw Jules source IDs;
- raw task/session titles;
- raw Activity IDs;
- Activity bodies/prompts/messages;
- `JULES_API_KEY` or any secret material.

It persists SHA-256 fingerprints, exact project repository identity, normalized provider state, activity counts/set fingerprints, safe activity-type/time metadata, and non-authoritative label/role hints only.

A fingerprint is an exact observation identity for reconciliation, but role/workstream hints never grant Writer/Reviewer ownership or mutation authority.

## Durable observation lanes

GS:

- lane: `ues-lane:v1|GS|GS|PROVIDER-OBSERVATION`
- ref: `ues-runtime/v2/lane/df79cc1b5ac694ed134f43ded3becf60831cdd276af859a601c1e23fa652f0ae`

CEP:

- lane: `ues-lane:v1|CEP|PERSONAL%3ACEP|PROVIDER-OBSERVATION`
- ref: `ues-runtime/v2/lane/d390fd737a91469956068ba284693815ba58b5f538c62f08c7007be930013eed`

Each ref contains `state.json` using the normal UES Git-ref StateStore contract.

## Attention classifications

Provider observation surfaces, but does not automatically mutate, these states:

- `WAITING_INPUT_REQUIRES_RECONCILIATION`
- `TERMINAL_FAILURE_REQUIRES_RECONCILIATION`
- `COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK`
- `PROVIDER_STATE_UNKNOWN`
- `PROVIDER_SESSION_IDENTITY_INCOMPLETE`

Normal waiting/completed/failed provider states create Parent attention, not a permanent red runtime failure. Missing/stale provider observation is a hard observer failure because the automation would otherwise silently lose provider visibility.

## Parent Controller use

GS and CEP Parent Controllers should read their governed project state and GitHub truth as before, then use the corresponding UES provider-observation ref as the fresh provider inventory source when direct Jules tooling is unavailable in the ChatGPT runtime.

A completed session must be reconciled against project lineage before deciding whether its output has already been consumed. A failed session must be reconciled before replacement. A waiting session must be reconciled against exact project authority before any continuation. Current account enumeration is not lifetime Jules task-budget proof.

## Authority

Observation capability is not mutation authority. The observer runs in `SHADOW`, clears actor bindings rather than guessing them, records `provider_mutation_authorized=false`, and cannot create Jules tasks or send provider messages.
