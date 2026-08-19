# Architecture v0

## Goal

Provide one semantic execution protocol that can serve many project types without turning every workspace into a bloated universal image.

## Layer model

```text
Chat / Control Center
        |
        v
Universal protocol + safety core
        |
        +-- capability discovery
        +-- exact-state preflight
        +-- authority envelope
        +-- evidence/checkpoint model
        +-- resource advisory
        |
        v
Project-family adapter(s)
        |
        v
Repository override / extensions
        |
        v
Repository-native commands + CI
```

## Extension rule

The universal core defines semantics, safety invariants, and evidence shapes. It does not need to understand every build tool.

Adapters map semantic operations such as `format-check`, `lint`, `test-fast`, or `build` to repository-native commands. Repository-specific extensions may add capabilities that do not belong in the universal core.

Unknown extension keys must be ignored by components that do not own them. This allows projects to evolve independently.

## Trust rule

A candidate repository contract is not automatically trusted merely because it exists in Git. Privileged execution must resolve the applicable authority source and trusted adapter/workflow version before executing candidate-provided commands with write credentials or secrets.

## Write safety target

Future mutating operations will require, at minimum:

- repository and ref identity;
- exact expected remote HEAD SHA;
- operation ID;
- workstream/authority identity;
- allowed path/resource scope;
- stop gate;
- compare-and-swap ref update;
- post-write changed-path and ancestry reconciliation;
- UNKNOWN-outcome recovery before retry.

The bootstrap release intentionally exposes no source-writing bridge command yet.

## CI responsibility

Workspace execution should provide fast feedback. Repository-native CI should provide clean exact-SHA verification and durable artifacts. Conversations should not be used as long-running polling loops.

## Resource intelligence

Resource optimization is capability-aware and telemetry-driven. A project may choose lean, balanced, or performance-oriented behavior. Prebuilds and caches are opt-in recommendations until evidence shows they improve total cost/time.

## Future phases

1. Validate read-only protocol and project discovery.
2. Add richer adapter registry and repository overrides.
3. Add exact-SHA preflight snapshot and evidence generation.
4. Add GitHub command bridge for read-only verification.
5. Add bounded mutating transactions only after recovery/idempotency tests.
6. Add portfolio-level telemetry aggregation and resource optimization recommendations.
