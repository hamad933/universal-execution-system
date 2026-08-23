# UES Automation Control Plane V2 — deterministic replay corpus

This directory is the Domain E test-only regression harness for Issue #9 / PR #10.
It contains only synthetic, sanitized fixtures and test infrastructure. It does not
persist project truth, call external providers, mutate GitHub/Jules, or define
production architecture.

## Running

Corpus-only validation on the frozen baseline:

```bash
python -m unittest discover -s tests/control_plane -p 'test_*.py' -v
```

Composed A-D integration expectation check:

```bash
UES_CONTROL_PLANE_INTEGRATION=1 \
python -m unittest discover -s tests/control_plane -p 'test_*.py' -v
```

The second command intentionally fails while required A-D modules are absent.
Those failures are integration expectations, not permission to weaken or delete a
scenario.

## Semantic protocol stubs

`protocols.py` records the exact semantic operations the replay adapter will need
once production interfaces freeze:

- Lifecycle: `resolve_next_transition(snapshot) -> decision`.
- Reconciliation: `reconcile_binding(runtime, observed) -> decision`.
- Routing: `route(snapshot) -> decision`.
- Watchdog: `evaluate(snapshot) -> incidents`.
- Task budget: `classify(snapshot) -> decision`.
- Jules provider: `normalize_state(raw_state) -> normalized_state`.
- Recovery: `recover_unknown_write(snapshot) -> decision`.
- Operation safety: `reserve_operation(operation_key, snapshot) -> decision`.

These are test-side `Protocol` stubs. Domains A-D do not need to copy these names
into production. After cross-domain interfaces are frozen, the integration authority
may bind concrete production APIs to these semantics without changing scenario
inputs, expected outcomes, safety properties, or replay intent.

## Locked safety properties

The corpus locks fail-closed binding, exact-SHA review freshness, exact CI artifact
binding, no blind mutation retry, duplicate-operation suppression, conservative task
budget behavior, independent-lane progress, forgotten-lane detection, same-writer
correction routing, mandatory re-CI/re-review after SHA movement, cycle failure for
untreated AUTO_SAFE incidents, and SHADOW default activation.
