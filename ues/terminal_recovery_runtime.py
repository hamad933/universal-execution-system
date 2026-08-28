from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Sequence

from . import terminal_recovery as recovery
from . import terminal_results
from .state_store import StateStoreError, StateUnavailable, record_authoritative_readback
from .task_budget_accounting import record_confirmed_generation

_DEFAULT_ACTIVITY_WORKERS = 4
_MAX_ACTIVITY_WORKERS = 8


def _activity_workers() -> int:
    raw = str(os.environ.get("UES_TERMINAL_BACKFILL_ACTIVITY_READ_WORKERS") or "").strip()
    if not raw:
        return _DEFAULT_ACTIVITY_WORKERS
    try:
        requested = int(raw)
    except ValueError:
        return _DEFAULT_ACTIVITY_WORKERS
    return max(1, min(_MAX_ACTIVITY_WORKERS, requested))


class _CachingReadOnlyClient:
    """Runtime-only provider cache; raw session identities never leave this process."""

    def __init__(
        self,
        delegate: Any,
        *,
        sources: list[Mapping[str, Any]] | None = None,
        sessions: list[Mapping[str, Any]] | None = None,
        source_error: BaseException | None = None,
        session_error: BaseException | None = None,
        activities: Mapping[str, tuple[bool, Any]] | None = None,
    ) -> None:
        self._delegate = delegate
        self._sources = sources
        self._sessions = sessions
        self._source_error = source_error
        self._session_error = session_error
        self._activities = dict(activities or {})

    def list_sources(self, *, page_size: int = 100) -> list[Mapping[str, Any]]:
        if self._source_error is not None:
            raise self._source_error
        if self._sources is not None:
            return list(self._sources)
        return self._delegate.list_sources(page_size=page_size)

    def list_sessions(self, *, page_size: int = 100) -> list[Mapping[str, Any]]:
        if self._session_error is not None:
            raise self._session_error
        if self._sessions is not None:
            return list(self._sessions)
        return self._delegate.list_sessions(page_size=page_size)

    def list_activities(self, session: str, *, page_size: int = 100) -> list[Mapping[str, Any]]:
        cached = self._activities.get(str(session))
        if cached is not None:
            ok, value = cached
            if ok:
                return list(value)
            raise value
        return self._delegate.list_activities(session, page_size=page_size)


def _exact_bound_needs_activity_read(
    store: Any,
    *,
    lineage: Mapping[str, Any],
    repository: str,
) -> bool:
    lane_id = str(lineage.get("lane_id") or "").strip()
    if not lane_id:
        return False
    lane_read = store.read_workstream(lane_id)
    if lane_read.status != "OK" or lane_read.record is None:
        return True
    evidence = lane_read.record.evidence_bindings or {}
    existing = evidence.get(recovery.TERMINAL_RESULT_KEY)
    if not isinstance(existing, Mapping):
        return True
    current = recovery._current_view(existing, evidence, repository)
    return current.get("result_state") != "PARENT_CONSUMABLE"


def _prefetch_exact_bound_activities(
    client: Any,
    *,
    store: Any,
    projects: Sequence[Mapping[str, str]],
    indexes: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    sources: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[bool, Any]], int]:
    """Read only already-exact-bound completed session Activities in bounded parallel.

    Pending/unresolved identities are deliberately excluded. They remain on the
    canonical sequential identity-reconciliation path and are read only after exact
    binding succeeds. StateStore CAS/persistence is never parallelized here.
    """

    project_by_repo = {
        str(item.get("repository") or "").casefold(): item
        for item in projects
        if str(item.get("repository") or "").strip()
    }
    source_by_name = {
        recovery._resource_name(source.get("name")): source
        for source in sources
        if recovery._resource_name(source.get("name"))
    }
    eligible: list[str] = []
    for session in sessions:
        if str(session.get("normalizedState") or "").upper() != "COMPLETED":
            continue
        session_name = recovery._resource_name(session.get("name"))
        if not session_name:
            continue
        source = source_by_name.get(recovery._resource_name(session.get("sourceIdentifier")))
        repository = str(source.get("repository") or "").strip() if isinstance(source, Mapping) else ""
        project = project_by_repo.get(repository.casefold())
        if project is None:
            continue
        fp = recovery.session_fingerprint(session_name)
        matches = list(indexes.get(str(project.get("project") or ""), {}).get(fp, ()))
        if len(matches) != 1:
            continue
        if not _exact_bound_needs_activity_read(store, lineage=matches[0], repository=repository):
            continue
        eligible.append(session_name)

    if not eligible:
        return {}, 0

    workers = min(_activity_workers(), len(eligible))

    def read_one(session_name: str) -> tuple[bool, Any]:
        try:
            return True, client.list_activities(session_name, page_size=100)
        except BaseException as exc:
            return False, exc

    if workers == 1:
        values = [read_one(session_name) for session_name in eligible]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ues-terminal-backfill") as executor:
            values = list(executor.map(read_one, eligible))
    return dict(zip(eligible, values, strict=True)), workers


def _exact_pending_operation_binding(
    store: Any,
    *,
    candidate: Mapping[str, Any],
    session_fp: str,
    bind: Any,
) -> dict[str, Any]:
    """Reconcile a pending initial-generation operation before binding its session.

    The caller has already proved a unique provider repository + starting-branch +
    exact transition-title-marker match. This wrapper additionally requires the
    durable operation identity/effect target to agree with that pending transition,
    records authoritative provider readback through the canonical operation state
    machine, then delegates lineage binding to the existing terminal recovery CAS.
    It never clears IN_FLIGHT/UNKNOWN state by itself and never repeats a provider
    effect.
    """

    lane_id = str(candidate.get("lane_id") or "").strip()
    pending = candidate.get("pending")
    snapshot = candidate.get("record")
    if not lane_id or not isinstance(pending, Mapping) or snapshot is None:
        return {
            "state": "IDENTITY_OPERATION_PROOF_INCOMPLETE",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    transition_key = str(pending.get("transition_key") or "").strip()
    starting_branch = str(pending.get("starting_branch") or "").strip()
    repository = str(pending.get("source_repository") or "").strip()
    generation = int(pending.get("next_generation") or 1)
    role = str((getattr(snapshot, "evidence_bindings", None) or {}).get("role") or "").upper()
    operation_key = str(getattr(snapshot, "operation_key", None) or "").strip()
    action = getattr(snapshot, "action_in_flight", None)
    if isinstance(action, Mapping):
        action_key = str(action.get("operation_key") or "").strip()
        if operation_key and action_key and operation_key != action_key:
            return {
                "state": "IDENTITY_OPERATION_KEY_MISMATCH",
                "cas_performed": False,
                "authoritative_readback": False,
            }
        operation_key = operation_key or action_key

    if not all((transition_key, starting_branch, repository, role, operation_key, session_fp)):
        return {
            "state": "IDENTITY_OPERATION_PROOF_INCOMPLETE",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    operation = store.read_operation(operation_key)
    if operation.status != "OK" or operation.record is None:
        return {
            "state": "IDENTITY_OPERATION_STATE_UNAVAILABLE",
            "cas_performed": False,
            "authoritative_readback": False,
        }
    op = operation.record
    effect = op.effect_identity if isinstance(op.effect_identity, Mapping) else {}
    target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
    exact_operation = (
        op.lane_id == lane_id
        and op.state in {"IN_FLIGHT", "UNKNOWN"}
        and str(effect.get("project") or "") == str(getattr(snapshot, "project", "") or "")
        and str(effect.get("route") or "") == str(getattr(snapshot, "route", "") or "")
        and str(target.get("creation_kind") or "").upper() == "INITIAL_LOGICAL_LINEAGE"
        and str(target.get("transition_key") or "") == transition_key
        and str(target.get("starting_branch") or "") == starting_branch
        and str(target.get("role") or "").upper() == role
        and int(target.get("generation") or 0) == generation
    )
    if not exact_operation:
        return {
            "state": "IDENTITY_OPERATION_EFFECT_MISMATCH",
            "cas_performed": False,
            "authoritative_readback": True,
        }

    try:
        record_authoritative_readback(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            observed=True,
            evidence={
                "outcome": "INITIAL_LINEAGE_SESSION_CREATED_RECONCILED_BY_TERMINAL_READBACK",
                "session_fingerprint": session_fp,
                "repository": repository,
                "starting_branch": starting_branch,
                "generation": generation,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "initial_lineage_transition_key": transition_key,
                "authoritative_provider_enumeration": True,
                "raw_session_id_persisted": False,
                "raw_title_persisted": False,
                "safe_to_blind_retry": False,
            },
        )
    except (StateStoreError, StateUnavailable):
        return {
            "state": "IDENTITY_OPERATION_READBACK_RECONCILIATION_REQUIRED",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    binding = bind(store, candidate=candidate, session_fp=session_fp)
    if binding.get("state") not in {
        "IDENTITY_EXACTLY_BOUND",
        "IDENTITY_ALREADY_BOUND",
        "IDENTITY_CONCURRENTLY_BOUND",
        "IDENTITY_BOUND_READBACK_RECONCILED",
    }:
        return binding

    try:
        accounting = record_confirmed_generation(
            store,
            project=str(getattr(snapshot, "project", "") or ""),
            route=str(getattr(snapshot, "route", "") or ""),
            operation_key=operation_key,
            generation_transition_key=transition_key,
        )
    except (StateStoreError, StateUnavailable):
        return {
            "state": "IDENTITY_BOUND_BUDGET_ACCOUNTING_RECONCILIATION_REQUIRED",
            "cas_performed": bool(binding.get("cas_performed")),
            "authoritative_readback": True,
        }
    return {**binding, "budget_accounting": accounting}


def run_read_only_backfill(
    project_names: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run terminal backfill with cached identity and bounded exact-bound Activity reads."""

    canonical_index = terminal_results.lineage_index
    original_results_index = terminal_results.lineage_index
    original_recovery_index = recovery.lineage_index
    original_bind = recovery._bind_pending_identity_once
    original_pending = recovery._pending_identity_candidates
    cache: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    pending_cache: dict[tuple[str, str, str], list[dict[str, Any]]] | None = None

    def cached_index(live_store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
        key = (str(project), str(route))
        try:
            value = canonical_index(live_store, project=project, route=route)
        except StateUnavailable:
            if key in cache:
                return cache[key]
            raise
        if value or key not in cache:
            cache[key] = {
                str(fp): [dict(item) for item in matches]
                for fp, matches in value.items()
            }
        return value

    def cached_pending(
        live_store: Any,
        projects: Sequence[Mapping[str, str]],
    ) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        nonlocal pending_cache
        try:
            value = original_pending(live_store, projects)
        except StateUnavailable:
            if pending_cache is not None:
                return pending_cache
            raise
        pending_cache = value
        return value

    def governed_bind(live_store: Any, *, candidate: Mapping[str, Any], session_fp: str) -> dict[str, Any]:
        return _exact_pending_operation_binding(
            live_store,
            candidate=candidate,
            session_fp=session_fp,
            bind=original_bind,
        )

    terminal_results.lineage_index = cached_index
    recovery.lineage_index = cached_index
    recovery._pending_identity_candidates = cached_pending
    recovery._bind_pending_identity_once = governed_bind
    prefetched_count = 0
    activity_workers = 0
    effective_store = store
    effective_client = client
    try:
        if effective_store is None:
            try:
                effective_store = recovery.build_live_state_store()
            except Exception:
                return recovery.run_read_only_backfill(
                    project_names,
                    store=store,
                    client=client,
                )

        projects = recovery.load_governed_projects(project_names)
        try:
            indexes = {
                item["project"]: cached_index(
                    effective_store,
                    project=item["project"],
                    route=item["route"],
                )
                for item in projects
            }
            cached_pending(effective_store, projects)
        except StateUnavailable:
            return recovery.run_read_only_backfill(
                project_names,
                store=effective_store,
                client=client,
            )

        if effective_client is None:
            key = str(os.environ.get("JULES_API_KEY") or "").strip()
            if key:
                effective_client = recovery.JulesClient(key)

        if effective_client is not None:
            sources: list[Mapping[str, Any]] | None = None
            sessions: list[Mapping[str, Any]] | None = None
            source_error: BaseException | None = None
            session_error: BaseException | None = None
            try:
                sources = list(effective_client.list_sources(page_size=100))
            except BaseException as exc:
                source_error = exc
            if source_error is None:
                try:
                    sessions = list(effective_client.list_sessions(page_size=100))
                except BaseException as exc:
                    session_error = exc

            activity_cache: dict[str, tuple[bool, Any]] = {}
            if source_error is None and session_error is None and sources is not None and sessions is not None:
                activity_cache, activity_workers = _prefetch_exact_bound_activities(
                    effective_client,
                    store=effective_store,
                    projects=projects,
                    indexes=indexes,
                    sources=sources,
                    sessions=sessions,
                )
                prefetched_count = len(activity_cache)

            effective_client = _CachingReadOnlyClient(
                effective_client,
                sources=sources,
                sessions=sessions,
                source_error=source_error,
                session_error=session_error,
                activities=activity_cache,
            )

        result = recovery.run_read_only_backfill(
            project_names,
            store=effective_store,
            client=effective_client,
        )
        result["prefetched_exact_bound_activity_read_count"] = prefetched_count
        result["terminal_activity_read_workers"] = activity_workers
        result["parallel_activity_reads_get_only"] = True
        return result
    finally:
        terminal_results.lineage_index = original_results_index
        recovery.lineage_index = original_recovery_index
        recovery._pending_identity_candidates = original_pending
        recovery._bind_pending_identity_once = original_bind
