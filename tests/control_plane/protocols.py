"""Exact R2 production binding inventory for Automation Control Plane replay.

Domain E is tests only. Names are frozen by Integration Authority. Missing symbols are
hard failures; production replay does not search aliases or fall back to the oracle.
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

R2_PUBLIC_BINDINGS = {
    "action_capability": "ues.lifecycle.ActionCapability",
    "actor_binding": "ues.reconciliation.ActorBinding",
    "required_evidence_profile": "ues.reconciliation.RequiredEvidenceProfile",
    "canonical_lane_key": "ues.reconciliation.canonical_lane_key",
    "resolve_actor_binding": "ues.reconciliation.resolve_actor_binding",
    "reconcile_workstream": "ues.reconciliation.reconcile_workstream",
    "reconcile_portfolio": "ues.reconciliation.reconcile_portfolio",
    "jules_normalize_state": "ues.providers.jules.normalize_session_state",
    "jules_send_message": "ues.providers.jules.JulesClient.send_message",
    "github_required_ci": "ues.providers.github.GitHubClient.get_required_ci_evidence",
    "github_workflow_binding": "ues.providers.github.GitHubClient.get_workflow_binding",
    "failure_cascade": "ues.failures.collapse_failure_cascade",
    "waiting_classifier": "ues.routing.classify_waiting_activity",
    "waiting_routing": "ues.routing.route_waiting",
    "reviewer_to_writer": "ues.routing.route_reviewer_to_writer",
    "writer_to_reviewer": "ues.routing.route_writer_to_reviewer",
    "terminal_session_routing": "ues.routing.route_terminal_session_failure",
    "control_cycle": "ues.watchdog.evaluate_control_cycle",
    "lane_state": "ues.state_store.WorkstreamRuntimeRecord",
    "effect_identity": "ues.idempotency.EffectIdentity",
    "state_claim": "ues.state_store.claim_operation",
}

R2_REQUIRED_SEMANTICS = (
    "one lane can carry independent Writer and Reviewer actor bindings",
    "unique heuristic actor or session binding remains unproven",
    "project_auto_safe_actions gates every external-effect route",
    "CI and review evidence-only waiting remains read-only",
    "required CI identity uses get_required_ci_evidence",
    "artifact evidence is run-attempt and producer bound or UNPROVEN",
    "base scope and evidence-profile drift reconciles before action",
    "FORGOTTEN_LANE makes the control cycle unhealthy",
    "valid Parent or Owner Stop Gate alone does not fail the cycle",
    "failure cascades collapse only with explicit common-root identity",
    "lane_id plus project route workstream binds state and effect identity",
    "same waiting Activity plus changed payload collides",
    "missing or unknown activation state remains SHADOW",
)
