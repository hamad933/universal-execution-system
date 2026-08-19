# Resource Policy v0

## Default posture: LEAN

The universal system must optimize for useful capability, not maximum installed capability.

1. Detect repository requirements before provisioning optional tools.
2. Load only required project-family adapters and explicitly enabled extensions.
3. Keep prebuilds OFF by default.
4. Cache only when measured bootstrap cost, dependency footprint, or repeated use justifies it.
5. Re-evaluate caches with low hit rates; storage is not free merely because it is inside a free allowance.
6. Prefer ephemeral/recyclable workspaces for infrequent projects.
7. Never use an execution workspace as the canonical long-term artifact store.
8. Never cache secrets, mutable trust state, or PASS conclusions across different source SHAs.
9. Resource recommendations are advisory until a project policy explicitly authorizes autonomous changes.
10. Project-specific overrides are allowed, but must not force unrelated projects to carry the same tools or storage cost.

## Telemetry inputs

Adapters and Control Centers may contribute normalized measurements such as:

- bootstrap duration;
- dependency footprint;
- execution frequency;
- cache hit/miss ratio;
- build/test duration;
- browser/tool download footprint;
- workspace size;
- last-used timestamp;
- prebuild startup savings.

The system should prefer measurements over assumptions and should attach rationale to every optimization recommendation.
