"""Test-only integration binding inventory for Automation Control Plane V2 replay.

The fixture semantics are authoritative. Concrete production names below describe the
reviewed A-D heads where a callable already exists. Missing semantic bindings must fail
integration mode until Integration Authority binds a corrected production API; they are
not permission to move production logic into tests.
"""
from __future__ import annotations

EXPECTED_PRODUCTION_MODULES = (
    "ues.lifecycle",
    "ues.reconciliation",
    "ues.providers.jules",
    "ues.providers.github",
    "ues.providers.base",
    "ues.routing",
    "ues.watchdog",
    "ues.task_budget",
    "ues.metrics",
    "ues.state_store",
    "ues.recovery",
    "ues.failures",
    "ues.idempotency",
    "ues.operation_records",
    "ues.transaction",
)

CURRENT_REVIEWED_BINDINGS = {
    "lifecycle_next": "ues.lifecycle.resolve_next_action",
    "reconcile_workstream": "ues.reconciliation.reconcile_workstream",
    "reconcile_portfolio": "ues.reconciliation.reconcile_portfolio",
    "jules_normalize_state": "ues.providers.jules.normalize_session_state",
    "jules_send_message": "ues.providers.jules.JulesClient.send_message",
    "github_required_ci": "ues.providers.github.GitHubClient.get_ci_evidence",
    "github_workflow_binding": "ues.providers.github.GitHubClient.get_workflow_binding",
    "github_pr_read": "ues.providers.github.GitHubClient.get_pull_request",
    "provider_unknown_write_recovery": "ues.recovery.reconcile_provider_write",
    "failure_classification": "ues.failures.classify_failure",
    "provider_failure_classification": "ues.failures.classify_provider_failure",
    "failure_scope": "ues.failures.scope_blocker",
    "waiting_routing": "ues.routing.route_waiting",
    "reviewer_to_writer": "ues.routing.route_reviewer_to_writer",
    "writer_to_reviewer": "ues.routing.route_writer_to_reviewer",
    "terminal_session_routing": "ues.routing.route_terminal_session_failure",
    "lane_watchdog": "ues.watchdog.evaluate_lane_watchdog",
    "control_cycle": "ues.watchdog.evaluate_control_cycle",
    "task_budget": "ues.task_budget.evaluate_task_budget",
    "runtime_state": "ues.state_store.StateStore / WorkstreamRuntimeRecord",
    "idempotency": "ues.idempotency.evaluate_idempotency",
    "waiting_answer_identity": "ues.idempotency.waiting_answer_operation_key",
}

SEMANTIC_BINDINGS_REQUIRING_CORRECTED_A_D = (
    "explicit/source-backed session binding proof",
    "required CI identity classification with REQUIRED_CI_MISSING",
    "attempt-bound artifact lineage including producer/digest",
    "project/route/workstream portfolio identity and duplicate-session detection",
    "base/head/scope drift reconciliation",
    "cascaded shared-failure collapse",
    "AWAITING_PLAN_APPROVAL Parent/policy mutation gate",
    "structured waiting classifier that does not use keyword-only shortcuts",
    "required browser/route-profile evidence gate",
    "exact CANARY grant authorization",
    "stable waiting-Activity external-effect identity independent of answer payload",
    "project-specific AUTO_SAFE allowlist enforcement",
)
