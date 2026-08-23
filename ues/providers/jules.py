from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable
from urllib.parse import quote, urlencode

from ues.recovery import reconcile_provider_write
from .base import (
    AuthenticationError, AuthorizationError, HttpTransport, NetworkError, NotFoundError,
    ProtocolError, RateLimitError, RetryPolicy, ServerError, SessionContinuationUnavailable,
    UrllibTransport, WriteOutcomeUnknown, encode_json, error_for_response, read_json_with_retries,
)

JULES_ENDPOINT = "https://jules.googleapis.com"
DOCUMENTED_SESSION_STATES = frozenset({
    "QUEUED", "PLANNING", "AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK",
    "IN_PROGRESS", "PAUSED", "FAILED", "COMPLETED",
})
DEFAULT_MUTATION_STATES = frozenset({"AWAITING_USER_FEEDBACK", "IN_PROGRESS"})


def normalize_session_state(value: Any) -> str:
    state = str(value or "").strip().upper()
    return state if state in DOCUMENTED_SESSION_STATES else "UNKNOWN"


def _readback_unavailable(exc: Exception) -> dict[str, Any]:
    category = getattr(exc, "category", "PROVIDER_READ_ERROR")
    return {
        "schema_version": "0.5",
        "verdict": "AUTHORITATIVE_READ_UNAVAILABLE",
        "safe_to_blind_retry": False,
        "retry_consideration": "BLOCKED_UNTIL_AUTHORITATIVE_READ",
        "post_session_state": "UNKNOWN",
        "evidence": {"read_error_category": str(category)},
    }


class JulesClient:
    def __init__(self, api_key: str, *, transport: HttpTransport | None = None,
                 endpoint: str = JULES_ENDPOINT, timeout: float = 15.0,
                 read_retry_policy: RetryPolicy | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 allowed_mutation_states: Iterable[str] | None = None) -> None:
        if not api_key:
            raise ValueError("api_key is required at runtime")
        allowed = DEFAULT_MUTATION_STATES if allowed_mutation_states is None else frozenset(
            normalize_session_state(v) for v in allowed_mutation_states
        )
        if not allowed <= DEFAULT_MUTATION_STATES:
            raise ValueError("allowed_mutation_states may only narrow AWAITING_USER_FEEDBACK/IN_PROGRESS")
        self._api_key, self._transport = api_key, transport or UrllibTransport()
        self._endpoint, self._timeout = endpoint.rstrip("/"), timeout
        self._read_retry_policy, self._sleeper = read_retry_policy or RetryPolicy(), sleeper
        self._allowed_mutation_states = frozenset(allowed)

    def __repr__(self) -> str:
        return f"JulesClient(endpoint={self._endpoint!r}, api_key=<redacted>)"

    @property
    def allowed_mutation_states(self) -> frozenset[str]:
        return self._allowed_mutation_states

    def get_session(self, session: str) -> dict[str, Any]:
        name = _resource_name(session, "sessions")
        payload = self._read_json(f"/v1alpha/{name}", operation="jules.sessions.get")
        if not isinstance(payload, dict):
            raise ProtocolError("Jules session response must be an object", operation="jules.sessions.get")
        return _normalize_session(payload)

    def list_sessions(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._list("/v1alpha/sessions", "sessions", page_size, "jules.sessions.list", _normalize_session)

    def get_source(self, source: str) -> dict[str, Any]:
        name = _resource_name(source, "sources")
        payload = self._read_json(f"/v1alpha/{name}", operation="jules.sources.get")
        if not isinstance(payload, dict):
            raise ProtocolError("Jules source response must be an object", operation="jules.sources.get")
        return _normalize_source(payload)

    def list_sources(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._list("/v1alpha/sources", "sources", page_size, "jules.sources.list", _normalize_source)

    def list_activities(self, session: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        name = _resource_name(session, "sessions")
        return self._list(f"/v1alpha/{name}/activities", "activities", page_size,
                          "jules.sessions.activities.list", dict)

    def get_session_source_binding(self, session: str, *, expected_repository: str | tuple[str, str] | None = None,
                                   heuristic_candidates: Sequence[str] = ()) -> dict[str, Any]:
        return self._binding_from_session(self.get_session(session), expected_repository, heuristic_candidates)

    def send_message(self, session: str, prompt: str, *, expected_repository: str | tuple[str, str] | None = None,
                     expected_source: str | None = None) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a non-empty string")
        name = _resource_name(session, "sessions")
        pre_session = self.get_session(name)
        state = pre_session["normalizedState"]
        if state in {"FAILED", "COMPLETED"}:
            raise SessionContinuationUnavailable("Jules session is terminal", operation="jules.sendMessage")
        if state not in self._allowed_mutation_states:
            raise ProtocolError(f"Jules state {state} is not mutation-capable", operation="jules.sendMessage")
        if expected_repository is None:
            raise ProtocolError("expected_repository is required before Jules mutation", operation="jules.sendMessage")
        binding = self._binding_from_session(pre_session, expected_repository, ())
        if not binding["proven"] or not binding["matches_expected_repository"]:
            raise ProtocolError("Jules session source/repository ownership is not proven", operation="jules.sendMessage")
        if expected_source is not None and binding["source"] != _resource_name(expected_source, "sources"):
            raise ProtocolError("Jules session source identifier mismatch", operation="jules.sendMessage")

        pre = self.list_activities(name)
        pre_ids = {_activity_id(a) for a in pre}
        try:
            response = self._transport.request("POST", self._url(f"/v1alpha/{name}:sendMessage"),
                headers=self._headers(json_body=True), body=encode_json({"prompt": prompt}), timeout=self._timeout)
        except NetworkError:
            return self._recover_ambiguous(name, prompt, pre_ids, "NETWORK_ERROR")
        if 200 <= response.status <= 299:
            try:
                recovery = self.recover_send_message_outcome(
                    name,
                    prompt,
                    pre_activity_ids=pre_ids,
                    write_outcome="CONFIRMED_HTTP",
                )
            except (
                AuthenticationError,
                AuthorizationError,
                NotFoundError,
                RateLimitError,
                ServerError,
                NetworkError,
                ProtocolError,
            ) as exc:
                raise WriteOutcomeUnknown(
                    "Jules sendMessage succeeded at HTTP layer but authoritative post-write readback failed",
                    operation="jules.sendMessage",
                    recovery={"initial_write_result": "HTTP_SUCCESS", **_readback_unavailable(exc)},
                ) from exc
            if recovery["verdict"] != "WRITE_CONFIRMED_BY_ACTIVITY":
                raise WriteOutcomeUnknown("Jules sendMessage lacked authoritative activity readback",
                                          operation="jules.sendMessage", recovery=recovery)
            return _receipt(name, recovery, binding, False)
        error = error_for_response(response, operation="jules.sendMessage")
        if isinstance(error, (AuthenticationError, AuthorizationError, NotFoundError)):
            raise error
        if isinstance(error, (RateLimitError, ServerError)):
            return self._recover_ambiguous(name, prompt, pre_ids, error.category,
                                           error.status_code, error.retry_after)
        raise error

    def recover_send_message_outcome(self, session: str, prompt: str, *, pre_activity_ids: Iterable[str],
                                     write_outcome: str = "WRITE_OUTCOME_UNKNOWN") -> dict[str, Any]:
        name = _resource_name(session, "sessions")
        post_session, post_activities = self.get_session(name), self.list_activities(name)
        return reconcile_provider_write({"write_outcome": write_outcome, "pre_activity_ids": sorted(set(pre_activity_ids)),
            "post_session_state": post_session["normalizedState"], "post_activities": post_activities,
            "expected_user_message": prompt, "authoritative_read_complete": True})

    def _recover_ambiguous(self, name: str, prompt: str, pre_ids: set[str], cause: str,
                           status_code: int | None = None, retry_after: float | None = None) -> dict[str, Any]:
        try:
            recovery = self.recover_send_message_outcome(name, prompt, pre_activity_ids=pre_ids)
        except (AuthenticationError, AuthorizationError, NotFoundError, RateLimitError, ServerError, NetworkError, ProtocolError) as exc:
            recovery = _readback_unavailable(exc)
        if recovery["verdict"] == "WRITE_CONFIRMED_BY_ACTIVITY":
            result = _receipt(name, recovery, None, True); result["initial_write_error"] = cause; return result
        raise WriteOutcomeUnknown("Jules mutation result is ambiguous", status_code=status_code, retry_after=retry_after,
                                  operation="jules.sendMessage", recovery={"initial_write_error": cause, **recovery})

    def _binding_from_session(self, session: Mapping[str, Any], expected_repository: str | tuple[str, str] | None,
                              heuristic_candidates: Sequence[str]) -> dict[str, Any]:
        expected = _repo(expected_repository) if expected_repository is not None else None
        source_name = session.get("sourceIdentifier")
        if not isinstance(source_name, str) or not source_name:
            candidates = [str(v) for v in heuristic_candidates if str(v)]
            return {"schema_version": "0.5", "session": session.get("name"), "source": None,
                    "repository": candidates[0] if len(candidates) == 1 else None,
                    "verification": "PROPOSED_UNVERIFIED" if len(candidates) == 1 else "UNVERIFIED",
                    "proven": False, "heuristic": bool(candidates), "heuristic_candidates": candidates,
                    "expected_repository": expected, "matches_expected_repository": False,
                    "authority_source": "HEURISTIC_ONLY" if candidates else "NONE"}
        source = self.get_source(source_name)
        repository = source.get("repository")
        proven = bool(source.get("explicitRepositoryIdentity"))
        return {"schema_version": "0.5", "session": session.get("name"), "source": source_name,
                "source_id": source.get("id"), "repository": repository,
                "github_owner": source.get("github_owner"), "github_repo": source.get("github_repo"),
                "starting_branch": session.get("sourceStartingBranch"),
                "verification": "PROVEN_EXPLICIT_SOURCE" if proven else "UNVERIFIED_SOURCE_RESOURCE",
                "proven": proven, "heuristic": False, "expected_repository": expected,
                "matches_expected_repository": bool(proven and expected and repository and repository.casefold() == expected.casefold()),
                "authority_source": "JULES_SOURCE_RESOURCE"}

    def _list(self, path: str, key: str, page_size: int, operation: str, normalizer: Callable[[Mapping[str, Any]], dict[str, Any]]) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        items, token, seen = [], None, set()
        while True:
            query: dict[str, str | int] = {"pageSize": page_size}
            if token: query["pageToken"] = token
            payload = self._read_json(f"{path}?{urlencode(query)}", operation=operation)
            if not isinstance(payload, dict) or not isinstance(payload.get(key, []), list):
                raise ProtocolError("provider list response has invalid shape", operation=operation)
            for raw in payload.get(key, []):
                if not isinstance(raw, dict): raise ProtocolError("provider list item has invalid shape", operation=operation)
                items.append(normalizer(raw))
            next_token = payload.get("nextPageToken")
            if not next_token: return items
            if not isinstance(next_token, str) or next_token in seen: raise ProtocolError("invalid pagination token", operation=operation)
            seen.add(next_token); token = next_token

    def _read_json(self, path: str, *, operation: str) -> Any:
        return read_json_with_retries(self._transport, "GET", self._url(path), headers=self._headers(), timeout=self._timeout,
                                      retry_policy=self._read_retry_policy, sleeper=self._sleeper, operation=operation)
    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        result = {"Accept": "application/json", "X-Goog-Api-Key": self._api_key}
        if json_body: result["Content-Type"] = "application/json"
        return result
    def _url(self, path: str) -> str: return f"{self._endpoint}{path}"


def _normalize_session(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload); result["normalizedState"] = normalize_session_state(payload.get("state")); result["stateAuthoritative"] = result["normalizedState"] != "UNKNOWN"
    ctx = payload.get("sourceContext") if isinstance(payload.get("sourceContext"), Mapping) else {}
    result["sourceIdentifier"] = ctx.get("source") if isinstance(ctx.get("source"), str) else None
    ghctx = ctx.get("githubRepoContext") if isinstance(ctx.get("githubRepoContext"), Mapping) else {}
    result["sourceStartingBranch"] = ghctx.get("startingBranch")
    return result


def _normalize_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload); gh = payload.get("githubRepo") if isinstance(payload.get("githubRepo"), Mapping) else {}
    owner, repo = gh.get("owner"), gh.get("repo"); explicit = bool(isinstance(owner, str) and owner and isinstance(repo, str) and repo)
    result.update({"github_owner": owner if isinstance(owner, str) else None, "github_repo": repo if isinstance(repo, str) else None,
                   "repository": f"{owner}/{repo}" if explicit else None, "explicitRepositoryIdentity": explicit})
    return result


def _resource_name(value: str, kind: str) -> str:
    text = str(value or "").strip().strip("/"); prefix = f"{kind}/"; ident = text[len(prefix):] if text.startswith(prefix) else text
    if not ident or "/" in ident: raise ValueError(f"resource must be an id or {kind}/{{id}}")
    return f"{kind}/{quote(ident, safe='')}"
def _repo(value: str | tuple[str, str]) -> str:
    if isinstance(value, tuple): value = f"{value[0]}/{value[1]}"
    text = str(value or "").strip().strip("/")
    if text.count("/") != 1 or any(not p for p in text.split("/")): raise ValueError("repository must be owner/repo")
    return text
def _activity_id(activity: Mapping[str, Any]) -> str: return str(activity.get("name") or activity.get("id") or "")
def _receipt(name: str, recovery: Mapping[str, Any], binding: Mapping[str, Any] | None, ambiguous: bool) -> dict[str, Any]:
    result = {"schema_version": "0.5", "provider": "JULES", "operation": "sendMessage", "session": name,
              "outcome": "DELIVERED_AFTER_AMBIGUOUS_WRITE" if ambiguous else "DELIVERED",
              "activity": recovery.get("matched_activity"), "safe_to_blind_retry": False}
    if binding: result.update({"source": binding.get("source"), "repository": binding.get("repository")})
    return result