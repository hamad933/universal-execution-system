# UES — Future Improvements, Tools, and Apps

Status: optional roadmap. This is **not** a backlog that must all be implemented.

UES v1 should stay small. Add a new tool only when a measured problem justifies the additional permissions, storage, cost, maintenance, or operational complexity.

## 1. Improvement rule

Before adding anything, answer:

1. What current failure mode or repeated manual burden does it remove?
2. Can the current GitHub Connector + GitHub Actions solve it safely enough?
3. Does it reduce total moving parts rather than increase them?
4. Can it preserve exact SHA, bounded write scope, idempotency, and recovery?
5. What is the removal plan if it provides little value?

Default decision when there is no measured pain: **do not add it yet**.

---

# Tier 1 — High-value improvements

These are the most useful future upgrades if the corresponding trigger appears.

## 2. Build a dedicated UES ChatGPT App / MCP server

### Why

The strongest usability improvement would be making UES available to any eligible ChatGPT chat as a small set of explicit tools rather than requiring the chat to reconstruct the protocol from prompts.

Suggested tools:

```text
ues.recover_state
ues.inspect_scope
ues.prepare_change
ues.apply_cas
ues.verify_post_state
ues.check_ci
ues.write_checkpoint
ues.integration_preflight
```

Do **not** expose generic shell execution. Keep semantic tools narrow.

### Trigger to add it

Add this when several projects/chats use UES regularly and copy-pasting the operator prompt becomes repetitive or inconsistent.

### Important current product note

As of 2026-08-20, OpenAI documents custom ChatGPT apps built with MCP. Full MCP write/modify actions are currently a beta capability for ChatGPT Business and Enterprise/Edu workspaces; availability and controls may change. If the workspace is not eligible, keep using the current GitHub connector/manual model rather than building around unavailable capabilities.

Official references:

- OpenAI — Apps in ChatGPT: https://help.openai.com/en/articles/11487775-connectors-in-chatgpt/
- OpenAI — Developer mode and MCP apps: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt

### Security shape

The MCP service should independently enforce:

- repository allowlist;
- exact expected SHA;
- path/resource write set;
- operation ID/idempotency;
- non-force CAS;
- post-write changed-path verification;
- no arbitrary user-provided shell;
- explicit integration/release boundaries.

The model should request an operation; the service should decide whether the operation satisfies the protocol.

---

## 3. Dedicated least-privilege GitHub App for UES

### Why

A UES-owned GitHub App would give a stable machine identity, explicit permissions, short-lived installation tokens, auditability, and cleaner separation from a human user's broad GitHub authority.

GitHub recommends selecting only the minimum permissions required by the app. Installation access tokens can also be restricted to repositories and to a subset of the app's granted permissions.

Official references:

- GitHub — Choosing permissions for a GitHub App: https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app
- GitHub — Installation access token API: https://docs.github.com/en/rest/apps/apps
- GitHub — GitHub App auth inside Actions: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow

### Suggested initial permissions

Start only with permissions actually needed, for example:

- repository metadata: read;
- contents: read/write only if the app itself performs bounded Git writes;
- pull requests: read, and write only if it must create/update PRs;
- issues: write only if receipts are stored as comments;
- actions/checks/statuses: add only if UES begins publishing native CI/check state.

Avoid administration permission unless a concrete feature truly requires it.

### Trigger to add it

Add when:

- UES must operate across many repositories;
- audit identity matters;
- connector permissions are too broad or inconsistent;
- UES needs to publish Checks/statuses itself;
- token lifecycle must be independent of a chat user's account.

---

## 4. Protect `main` with GitHub Rulesets

### Why

The repository currently relies heavily on operator discipline. GitHub rulesets can convert important invariants into server-enforced repository policy.

Recommended eventual `main` rules:

- require a pull request before merge;
- require the canonical validation check;
- block force pushes;
- restrict deletion of the default branch;
- optionally require additional security/code-quality checks later.

GitHub rulesets support required PRs, required status checks, blocking force pushes, signed commits, code scanning results, path restrictions, and more.

Official reference:

- GitHub — Available rules for rulesets: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets

### Important UES caveat

Do **not** require signed commits until every approved UES write backend can create commits GitHub recognizes as verified. The current Connector Backend pilot commit was intentionally validated by ancestry/CAS and CI, not by commit signature.

### Trigger to add it

Add rulesets once the normal branch/PR workflow is stable enough that server-side enforcement will not block legitimate recovery paths.

---

## 5. Dependabot for security and routine dependency maintenance

### Why

Dependency maintenance is repetitive and easy to automate independently of UES product logic.

Dependabot can raise security-update PRs for known vulnerable dependencies and version-update PRs for routine dependency maintenance.

Official references:

- GitHub — Dependabot version updates: https://docs.github.com/en/code-security/concepts/supply-chain-security/dependabot-version-updates
- GitHub — Configuring Dependabot security updates: https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates

### Recommended UES relationship

Dependabot creates candidates; UES can:

```text
recover candidate
→ inspect dependency delta
→ run bounded validation/security audit
→ classify failure
→ prepare integration evidence
```

Do not automatically merge broad dependency changes merely because CI is green.

### Trigger to add it

Add when a repository has real package dependencies and manual update/security PRs are consuming time.

---

## 6. Measured GitHub Actions caching

GitHub dependency caching can reduce repeated dependency-download time and cost. GitHub also warns that caches should be treated as untrusted input and should never contain secrets.

Official reference:

- GitHub — Dependency caching: https://docs.github.com/en/actions/concepts/workflows-and-actions/dependency-caching

UES already has the correct policy: cache only when bootstrap cost, dependency size, and repeated usage prove a benefit.

### Trigger to add it

Use measured thresholds such as:

- repeated setup/download dominates job time;
- cache hit rate is healthy;
- storage churn is acceptable;
- the cache does not cross an unsafe trust boundary.

Never use cache merely because the workflow supports it.

---

# Tier 2 — Add only when UES usage grows

## 7. Native GitHub Check Runs / status reporting

Today durable PR comments/checkpoints are enough for a small system.

A dedicated GitHub App could later publish one canonical UES check per operation, for example:

```text
UES / operation
PLANNED
PREFLIGHTED
EXECUTING
CONFIRMED
REJECTED
UNKNOWN_REQUIRES_RECONCILIATION
```

Benefits:

- visible directly in the PR checks UI;
- machine-queryable status;
- easier ruleset integration;
- fewer long comment threads;
- strong app identity for who produced the status.

### Trigger to add it

Add when operation history across many PRs becomes difficult to understand using comments alone.

---

## 8. OpenTelemetry observability

OpenTelemetry is a vendor-neutral framework for traces, metrics, and logs.

Official reference:

- OpenTelemetry documentation: https://opentelemetry.io/docs/

Potential UES signals:

```text
operation duration
GitHub API call latency
CAS conflict rate
recovery count
UNKNOWN outcome count
CI wait duration
cache hit/miss
per-backend success rate
```

### Trigger to add it

Do not instrument UES simply because observability is good engineering practice. Add OpenTelemetry when:

- there is a long-running UES service/App/MCP backend;
- multiple users/projects depend on it;
- failures cannot be diagnosed reliably from GitHub evidence;
- latency/cost optimization needs real measurements.

For the current small repository-only UES, GitHub evidence is sufficient.

---

## 9. Temporal or another durable workflow engine

Temporal provides durable workflow execution designed to resume after crashes, network failures, or infrastructure outages.

Official reference:

- Temporal documentation: https://docs.temporal.io/

Potential future use:

```text
recover state
→ wait for external approval
→ launch CI
→ wait hours/days
→ reconcile result
→ apply bounded mutation
→ wait for deployment
→ verify
```

### Trigger to add it

Only consider a workflow engine when UES has many long-lived multi-system operations that cannot be represented cleanly by GitHub-native workflows/checkpoints.

Do **not** add Temporal for ordinary repository edits, CI, or short operations. It would be unnecessary infrastructure for current UES v1.

---

## 10. Self-hosted runners / Actions Runner Controller

GitHub self-hosted runners give control over hardware, network, operating system, and installed software, but the operator owns machine maintenance. GitHub-hosted runners already autoscale and are lower-maintenance for many workloads. GitHub recommends Actions Runner Controller as its Kubernetes-based reference for autoscaling self-hosted runners.

Official reference:

- GitHub — Self-hosted runners: https://docs.github.com/en/actions/concepts/runners/self-hosted-runners
- GitHub — Self-hosted runner reference / ARC: https://docs.github.com/en/actions/reference/runners/self-hosted-runners

### Trigger to add it

Use self-hosted runners only if one or more are true:

- jobs need access to a private network;
- specialized hardware is needed;
- GitHub-hosted runner limits materially block work;
- measured sustained workload makes owned infrastructure cheaper;
- a controlled preinstalled toolchain provides a major performance win.

For sporadic UES work, stay on GitHub-hosted runners.

---

## 11. Renovate — alternative for complex dependency automation

Renovate can scan package files, raise dependency PRs, group updates, and optionally automerge selected low-risk updates after required tests pass.

Official references:

- Renovate use cases: https://docs.renovatebot.com/getting-started/use-cases/
- Renovate automerge: https://docs.renovatebot.com/key-concepts/automerge/

### Trigger to add it

Prefer Dependabot first when GitHub-native dependency maintenance is sufficient.

Consider Renovate instead when projects need advanced grouping, scheduling, custom managers, broad monorepo handling, or dependency policies that Dependabot does not express cleanly.

Do not run Dependabot and Renovate with overlapping responsibility by default.

---

# Tier 3 — Process improvements, not infrastructure

## 12. Standard repository UES contract

For each onboarded project, add a tiny repository-owned contract such as:

```text
.ues/project.json
```

Possible fields:

```json
{
  "family": "web",
  "default_branch": "main",
  "trusted_checks": ["Core CI"],
  "write_domains": {
    "frontend": ["resources/js/**"],
    "backend": ["app/**", "tests/**"]
  },
  "prohibited_paths": [".github/workflows/**"],
  "commands": {
    "test-fast": ["..."],
    "format-check": ["..."]
  }
}
```

The contract should describe repository facts and safe command mappings. It should not be able to grant itself global UES authority.

### Trigger to add it

Add when onboarding the second or third real project and repeated discovery becomes wasteful.

---

## 13. A tiny UES onboarding checklist

Each new repository should answer once:

- default branch;
- package/runtime families;
- canonical CI checks;
- branch/PR governance;
- test commands;
- format commands;
- protected files;
- common write domains;
- dependency/security tooling;
- release/deploy boundary;
- whether the connector can write;
- whether Actions or another backend is required.

Store only stable facts. Always recover current SHAs live.

---

## 14. Better operation receipts

The current minimum receipt is enough:

```text
operation ID
repository
branch/PR
start SHA
final SHA
changed paths
backend
validation run/result
outcome
```

Later, if needed, add:

- transformation digest;
- authority reference;
- tool/backend version;
- timings;
- recovery links;
- CI evidence IDs.

Avoid building a database until GitHub-native receipts become an actual scaling bottleneck.

---

# Recommended adoption order

If future usage grows, use this order:

```text
NOW
UES v1 manual + GitHub Connector Backend + GitHub CI

NEXT HIGH VALUE
1. repository UES contracts for real projects
2. main-branch rulesets
3. Dependabot where dependencies exist
4. dedicated least-privilege GitHub App when identity/permissions need hardening

WHEN CHAT USAGE BECOMES REPETITIVE
5. custom UES ChatGPT App / MCP tools, if the workspace supports the required actions
6. native GitHub Check Run operation receipts

ONLY AFTER MEASUREMENT JUSTIFIES IT
7. caching improvements
8. OpenTelemetry
9. self-hosted runners / ARC
10. Temporal or another durable workflow engine
```

## What not to add yet

Do not add the following to UES v1 without a concrete trigger:

- Kubernetes;
- a persistent database;
- a message queue;
- a workflow engine;
- a separate secrets platform;
- a dedicated observability backend;
- self-hosted runners;
- multiple overlapping dependency bots;
- generic shell-execution APIs;
- broad autonomous merge/deploy permissions.

The system is strongest when the universal core stays small and capabilities are added only when discovered need justifies them.

## Final principle

Future UES development should optimize for:

```text
less manual reconstruction
+ fewer permissions
+ fewer retries
+ stronger exact-state guarantees
+ better evidence
+ lower execution cost
- unnecessary infrastructure
```

A tool is an improvement only if the total system becomes easier to operate and recover.
