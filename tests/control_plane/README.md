# UES Automation Control Plane V2 — deterministic replay gate

This directory is Domain E only. All fixtures are synthetic and sanitized. The harness
must not call live GitHub/Jules services, persist project truth, weaken tests, or define
production authority.

## Test modes

Normal isolated corpus validation:

```bash
python -m unittest discover -s tests/control_plane -p 'test_*.py' -v
```

Composed production-backed validation:

```bash
UES_CONTROL_PLANE_INTEGRATION=1 \
python -m unittest discover -s tests/control_plane -p 'test_*.py' -v
```

Normal mode validates fixture completeness, determinism, sanitization, the independent
`ReferenceOracle`, and that every fixture kind has a production adapter. Production
execution tests are skipped.

Integration mode is deliberately fail-hard. For every fixture it performs:

`fixture -> test-side adapter -> actual A-D production callable -> normalized actual -> fixture expected`

Missing modules, missing semantic bindings, production exceptions, and wrong normalized
results are test failures. There is no fallback from the production adapter to the
`ReferenceOracle`.

## Reference oracle vs production adapter

`replay_harness.py` contains the independent semantic expectation oracle. It imports no
`ues` production modules.

`production_adapters.py` contains thin translations to the reviewed A-D APIs. It does
not import or call the reference oracle. Provider tests use synthetic transport doubles
at the network boundary while executing the real provider methods.

`protocols.py` records the reviewed concrete bindings and the semantic capabilities that
remain unavailable until corrected A-D interfaces are composed. Integration Authority
may update only the test adapter binding for a frozen corrected interface; fixture input,
expected output, and safety intent must not be weakened to obtain green tests.

## R1 P0 additions

The corpus now includes required-CI identity, workflow run-attempt/artifact lineage,
explicit vs heuristic session proof, duplicate provider session detection, cross-project
lane identity, base/head/scope drift, mixed and cascaded CI causes, closed-unmerged PRs,
`AWAITING_PLAN_APPROVAL`, keyword false positives, SHADOW trigger enforcement, browser
route/profile evidence, restart-after-send recovery, correction re-CI/re-review,
waiting-Activity effect deduplication, exact CANARY grant mismatch, and project-specific
`ENVIRONMENT_MISMATCH` authority.

A unique heuristic Writer session is `PROPOSED_UNVERIFIED`; only explicit/source-backed
binding can be `PROVEN`.

## Safety invariants locked by the suite

The suite locks fail-closed binding, exact candidate/evidence freshness, required-CI
identity, attempt-bound artifacts, no blind mutation retry, durable effect deduplication,
conservative task budget handling, blocked-lane isolation, `FORGOTTEN_LANE`, same-Writer
correction routing, mandatory re-CI/re-review after a new SHA, control-cycle failure only
for untreated proven AUTO_SAFE incidents, and SHADOW as the default when activation
authority is missing.
