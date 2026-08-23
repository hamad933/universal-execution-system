from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .reconciliation import EvidenceRequirement, RequiredEvidenceProfile


class ProjectAdapterError(ValueError):
    pass


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProjectAdapterError(f"{name} is required")
    return text


@dataclass(frozen=True)
class EvidenceRequirementSpec:
    requirement_id: str
    provider: str
    workflow: str
    job: str | None
    exact_candidate_sha: bool
    artifact_attempt_bound: bool
    route_profile_required_when_applicable: bool
    required: bool


@dataclass(frozen=True)
class EvidenceProfileSpec:
    profile_id: str
    requirements: tuple[EvidenceRequirementSpec, ...]


@dataclass(frozen=True)
class ProjectAdapter:
    schema_version: str
    adapter_id: str
    project: str
    route: str
    repository: str
    default_mode: str
    mutation_allowed: bool
    runtime_mode_is_authority: bool
    project_auto_safe_actions: tuple[str, ...]
    new_task_authority: str
    unknown_lifetime_capacity: str
    automatic_new_task_creation: bool
    waiting_classifier_rules: Mapping[str, Any]
    evidence_profiles: Mapping[str, EvidenceProfileSpec]
    raw: Mapping[str, Any]

    @property
    def config_grants_mutation_authority(self) -> bool:
        # Project configuration can narrow policy but can never be an activation
        # authority event by itself. The current shared loop remains SHADOW-only.
        return False

    def evidence_profile_spec(self, name: str) -> EvidenceProfileSpec:
        try:
            return self.evidence_profiles[name]
        except KeyError as exc:
            raise ProjectAdapterError(f"unknown evidence profile: {name}") from exc


def _parse_evidence_profiles(value: Any) -> dict[str, EvidenceProfileSpec]:
    if not isinstance(value, Mapping) or not value:
        raise ProjectAdapterError("evidence_profiles must be a non-empty object")
    profiles: dict[str, EvidenceProfileSpec] = {}
    for profile_name, raw_profile in value.items():
        if not isinstance(raw_profile, Mapping):
            raise ProjectAdapterError(f"evidence profile {profile_name} must be an object")
        profile_id = _required_text(raw_profile.get("profile_id"), f"{profile_name}.profile_id")
        raw_requirements = raw_profile.get("requirements")
        if not isinstance(raw_requirements, list) or not raw_requirements:
            raise ProjectAdapterError(f"{profile_name}.requirements must be non-empty")
        requirements: list[EvidenceRequirementSpec] = []
        for index, raw_requirement in enumerate(raw_requirements):
            if not isinstance(raw_requirement, Mapping):
                raise ProjectAdapterError(
                    f"{profile_name}.requirements[{index}] must be an object"
                )
            provider = _required_text(
                raw_requirement.get("provider"),
                f"{profile_name}.requirements[{index}].provider",
            )
            workflow = _required_text(
                raw_requirement.get("workflow"),
                f"{profile_name}.requirements[{index}].workflow",
            )
            job_value = raw_requirement.get("job")
            job = str(job_value).strip() if job_value is not None else None
            requirement_id = ":".join(
                part for part in (provider, workflow, job) if part
            )
            requirements.append(
                EvidenceRequirementSpec(
                    requirement_id=requirement_id,
                    provider=provider,
                    workflow=workflow,
                    job=job,
                    exact_candidate_sha=bool(raw_requirement.get("exact_candidate_sha")),
                    artifact_attempt_bound=bool(raw_requirement.get("artifact_attempt_bound")),
                    route_profile_required_when_applicable=bool(
                        raw_requirement.get("route_profile_required_when_applicable")
                    ),
                    required=bool(raw_requirement.get("required", True)),
                )
            )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ProjectAdapterError(f"duplicate evidence requirement in {profile_name}")
        profiles[str(profile_name)] = EvidenceProfileSpec(
            profile_id=profile_id,
            requirements=tuple(requirements),
        )
    return profiles


def parse_project_adapter(value: Mapping[str, Any]) -> ProjectAdapter:
    if not isinstance(value, Mapping):
        raise ProjectAdapterError("adapter must be an object")
    if value.get("adapter_kind") != "portfolio-project-control":
        raise ProjectAdapterError("unsupported adapter_kind")

    lane = value.get("canonical_lane")
    if not isinstance(lane, Mapping):
        raise ProjectAdapterError("canonical_lane is required")
    if lane.get("components") != ["project", "route", "workstream"]:
        raise ProjectAdapterError("canonical lane must be (project, route, workstream)")
    if bool(lane.get("allow_bare_workstream_key")):
        raise ProjectAdapterError("bare workstream durable identity is forbidden")

    activation = value.get("activation")
    if not isinstance(activation, Mapping):
        raise ProjectAdapterError("activation is required")
    default_mode = _required_text(activation.get("default_mode"), "activation.default_mode").upper()
    if default_mode not in {"SHADOW", "CANARY", "ACTIVE_AUTO_SAFE"}:
        raise ProjectAdapterError("unknown activation mode")
    if bool(activation.get("runtime_mode_is_authority")):
        raise ProjectAdapterError("runtime activation mode must never be authority")

    owners = value.get("truth_owners")
    if not isinstance(owners, Mapping):
        raise ProjectAdapterError("truth_owners is required")
    if owners.get("governed_state") != "DRIVE":
        raise ProjectAdapterError("governed state owner must be DRIVE")
    if owners.get("technical_state") != "GITHUB":
        raise ProjectAdapterError("technical state owner must be GITHUB")
    if owners.get("provider_state") != "PROVIDER":
        raise ProjectAdapterError("provider state owner must be PROVIDER")

    actor = value.get("actor_binding")
    if not isinstance(actor, Mapping):
        raise ProjectAdapterError("actor_binding is required")
    roles = actor.get("roles")
    if roles != ["WRITER", "REVIEWER"]:
        raise ProjectAdapterError("adapter must preserve WRITER and REVIEWER roles")
    if actor.get("external_effect_proof_required") != "PROVEN_EXPLICIT":
        raise ProjectAdapterError("external effects require PROVEN_EXPLICIT actor proof")
    if actor.get("heuristic_match_status") != "PROPOSED_UNVERIFIED":
        raise ProjectAdapterError("heuristic actor match must remain unverified")
    if not bool(actor.get("source_repository_must_match")):
        raise ProjectAdapterError("actor source repository match is mandatory")

    raw_actions = value.get("project_auto_safe_actions")
    if not isinstance(raw_actions, list):
        raise ProjectAdapterError("project_auto_safe_actions must be a list")
    actions = tuple(dict.fromkeys(str(item).strip().upper() for item in raw_actions if str(item).strip()))

    task_budget = value.get("task_budget")
    if not isinstance(task_budget, Mapping):
        raise ProjectAdapterError("task_budget is required")
    new_task_authority = _required_text(
        task_budget.get("new_task_authority"), "task_budget.new_task_authority"
    ).upper()
    if new_task_authority != "PARENT_ONLY":
        raise ProjectAdapterError("new task authority must remain PARENT_ONLY")
    unknown_capacity = _required_text(
        task_budget.get("unknown_lifetime_capacity"),
        "task_budget.unknown_lifetime_capacity",
    ).upper()
    if unknown_capacity != "DENY":
        raise ProjectAdapterError("unknown lifetime task capacity must fail closed")
    if bool(task_budget.get("automatic_new_task_creation")):
        raise ProjectAdapterError("automatic new task creation is forbidden")

    classifier = value.get("waiting_classifier")
    if not isinstance(classifier, Mapping):
        raise ProjectAdapterError("waiting_classifier is required")
    if bool(classifier.get("keyword_shortcuts_allowed")):
        raise ProjectAdapterError("keyword waiting classification is forbidden")
    if classifier.get("unmatched") != "UNCLASSIFIED":
        raise ProjectAdapterError("unmatched waiting evidence must be UNCLASSIFIED")
    rules = classifier.get("rules")
    if not isinstance(rules, list):
        raise ProjectAdapterError("waiting_classifier.rules must be a list")

    return ProjectAdapter(
        schema_version=_required_text(value.get("schema_version"), "schema_version"),
        adapter_id=_required_text(value.get("adapter_id"), "adapter_id"),
        project=_required_text(value.get("project"), "project"),
        route=_required_text(value.get("route"), "route"),
        repository=_required_text(value.get("repository"), "repository"),
        default_mode=default_mode,
        mutation_allowed=bool(activation.get("mutation_allowed")),
        runtime_mode_is_authority=False,
        project_auto_safe_actions=actions,
        new_task_authority=new_task_authority,
        unknown_lifetime_capacity=unknown_capacity,
        automatic_new_task_creation=False,
        waiting_classifier_rules={"rules": list(rules)},
        evidence_profiles=_parse_evidence_profiles(value.get("evidence_profiles")),
        raw=dict(value),
    )


def load_project_adapter(path: str | Path) -> ProjectAdapter:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectAdapterError(f"adapter could not be loaded: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ProjectAdapterError("adapter JSON must contain an object")
    return parse_project_adapter(value)


def build_required_evidence_profile(
    adapter: ProjectAdapter,
    profile_name: str,
    observations: Mapping[str, Mapping[str, Any]] | None = None,
) -> RequiredEvidenceProfile:
    spec = adapter.evidence_profile_spec(profile_name)
    observed = observations or {}
    requirements: list[EvidenceRequirement] = []
    for requirement in spec.requirements:
        evidence = observed.get(requirement.requirement_id)
        evidence = evidence if isinstance(evidence, Mapping) else {}
        requirements.append(
            EvidenceRequirement(
                name=requirement.requirement_id,
                proven=bool(evidence.get("proven")) if requirement.required else True,
                current=bool(evidence.get("current", False)) if requirement.required else True,
                evidence_id=(
                    str(evidence.get("evidence_id")).strip()
                    if evidence.get("evidence_id") is not None
                    else None
                ),
            )
        )
    return RequiredEvidenceProfile(spec.profile_id, tuple(requirements))
