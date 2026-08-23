from __future__ import annotations

import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from ues.recovery import reconcile_provider_write

from .base import (
    AuthenticationError,
    AuthorizationError,
    HttpTransport,
    NetworkError,
    NotFoundError,
    ProtocolError,
    RateLimitError,
    RetryPolicy,
    ServerError,
    SessionContinuationUnavailable,
    UrllibTransport,
    WriteOutcomeUnknown,
    encode_json,
    error_for_response,
    read_json_with_retries,
)

JULES_ENDPOINT = "https://jules.googleapis.com"
DOCUMENTED_SESSION_STATES = frozenset(
    {
        "QUEUED",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_FEEDBACK",
        "IN_PROGRESS",
        "PAUSED",
        "FAILED",
        "COMPLETED",
    }
)


def normalize_session_state(value: Any) -> str:
    state = str(value or "").strip().upper()
    return state if state in DOCUMENTED_SESSION_STATES else "UNKNOWN"


class JulesClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
        endpoint: str = JULES_ENDPOINT,
        timeout: float = 15.0,
        read_retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required at runtime")
        self._api_key = api_key
        self._transport = transport or UrllibTransport()
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._read_retry_policy = read_retry_policy or RetryPolicy()
        self._sleeper = sleeper

    def __repr__(self) -> str:
        return f"JulesClient(endpoint={self._endpoint!r}, api_key=<redacted>)"

    def get_session(self, session: str) -> dict[str, Any]:
        name = _session_name(session)
        payload = self._read_json(f"/v1alpha/{name}", operation="jules.sessions.get")
        if not isinstance(payload, dict):
            raise ProtocolError("Jules session response must be an object", operation="jules.sessions.get")
        result = dict(payload)
        result["normalizedState"] = normalize_session_state(payload.get("state"))
        result["stateAuthoritative"] = result["normalizedState"] != "UNKNOWN"
        return result

    def list_sessions(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._list_paginated(
            "/v1alpha/sessions",
            item_key="sessions",
            page_size=page_size,
            operation="jules.sessions.list",
            normalize_session=True,
        )

    def list_activities(self, session: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        name = _session_name(session)
        return self._list_paginated(
            f"/v1alpha/{name}/activities",
            item_key="activities",
            page_size=page_size,
            operation="jules.sessions.activities.list",
            normalize_session=False,
        )

    def send_message(self, session: str, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a non-empty string")
        name = _session_name(session)

        pre_session = self.get_session(name)
        if pre_session["normalizedState"] == "UNKNOWN":
            raise ProtocolError("Jules session state is unknown; mutation is fail-closed", operation="jules.sendMessage")
        if pre_session["normalizedState"] in {"FAILED", "COMPLETED"}:
            raise SessionContinuationUnavailable(
                "Jules session is terminal and cannot be continued", operation="jules.sendMessage"
            )
        pre_activities = self.list_activities(name)
        pre_ids = {_activity_identity(item) for item in pre_activities}

        url = self._url(f"/v1alpha/{name}:sendMessage")
        body = encode_json({"prompt": prompt})
        try:
            response = self._transport.request(
                "POST",
                url,
                headers=self._headers(json_body=True),
                body=body,
                timeout=self._timeout,
            )
        except NetworkError:
            return self._recover_ambiguous_send(name, prompt, pre_ids, cause="NETWORK_ERROR")

        if 200 <= response.status <= 299:
            post_session = self.get_session(name)
            post_activities = self.list_activities(name)
            recovery = reconcile_provider_write(
                {
                    "write_outcome": "CONFIRMED_HTTP",
                    "pre_activity_ids": sorted(pre_ids),
                    "post_session_state": post_session["normalizedState"],
                    "post_activities": post_activities,
                    "expected_user_message": prompt,
                    "authoritative_read_complete": True,
                }
            )
            if recovery["verdict"] != "WRITE_CONFIRMED_BY_ACTIVITY":
                raise WriteOutcomeUnknown(
                    "Jules sendMessage lacked authoritative activity readback",
                    operation="jules.sendMessage",
                    recovery=recovery,
                )
            return _delivery_receipt(name, recovery, ambiguous=False)

        error = error_for_response(response, operation="jules.sendMessage")
        if isinstance(error, (AuthenticationError, AuthorizationError, NotFoundError)):
            raise error
        if isinstance(error, (RateLimitError, ServerError)):
            return self._recover_ambiguous_send(
                name,
                prompt,
                pre_ids,
                cause=error.category,
                status_code=error.status_code,
                retry_after=error.retry_after,
            )
        raise error

    def recover_send_message_outcome(
        self,
        session: str,
        prompt: str,
        *,
        pre_activity_ids: list[str] | set[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        name = _session_name(session)
        post_session = self.get_session(name)
        post_activities = self.list_activities(name)
        return reconcile_provider_write(
            {
                "write_outcome": "WRITE_OUTCOME_UNKNOWN",
                "pre_activity_ids": sorted(set(pre_activity_ids)),
                "post_session_state": post_session["normalizedState"],
                "post_activities": post_activities,
                "expected_user_message": prompt,
                "authoritative_read_complete": True,
            }
        )

    def _recover_ambiguous_send(
        self,
        session_name: str,
        prompt: str,
        pre_ids: set[str],
        *,
        cause: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> dict[str, Any]:
        try:
            recovery = self.recover_send_message_outcome(
                session_name,
                prompt,
                pre_activity_ids=pre_ids,
            )
        except (AuthenticationError, AuthorizationError, NotFoundError, RateLimitError, ServerError, NetworkError, ProtocolError) as read_error:
            recovery = {
                "schema_version": "0.4",
                "verdict": "AUTHORITATIVE_READ_UNAVAILABLE",
                "safe_to_blind_retry": False,
                "retry_consideration": "BLOCKED_UNTIL_AUTHORITATIVE_READ",
                "post_session_state": "UNKNOWN",
                "evidence": {"read_error_category": read_error.category},
            }
        if recovery["verdict"] == "WRITE_CONFIRMED_BY_ACTIVITY":
            receipt = _delivery_receipt(session_name, recovery, ambiguous=True)
            receipt["initial_write_error"] = cause
            return receipt
        raise WriteOutcomeUnknown(
            "Jules mutation result is ambiguous",
            status_code=status_code,
            retry_after=retry_after,
            operation="jules.sendMessage",
            recovery={"initial_write_error": cause, **recovery},
        )

    def _list_paginated(
        self,
        path: str,
        *,
        item_key: str,
        page_size: int,
        operation: str,
        normalize_session: bool,
    ) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            query: dict[str, str | int] = {"pageSize": page_size}
            if page_token:
                query["pageToken"] = page_token
            payload = self._read_json(f"{path}?{urlencode(query)}", operation=operation)
            if not isinstance(payload, dict) or not isinstance(payload.get(item_key, []), list):
                raise ProtocolError("provider list response has invalid shape", operation=operation)
            for raw in payload.get(item_key, []):
                if not isinstance(raw, dict):
                    raise ProtocolError("provider list item has invalid shape", operation=operation)
                item = dict(raw)
                if normalize_session:
                    item["normalizedState"] = normalize_session_state(item.get("state"))
                    item["stateAuthoritative"] = item["normalizedState"] != "UNKNOWN"
                items.append(item)
            next_token = payload.get("nextPageToken")
            if not next_token:
                return items
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise ProtocolError("provider pagination token is invalid or repeated", operation=operation)
            seen_tokens.add(next_token)
            page_token = next_token

    def _read_json(self, path: str, *, operation: str) -> Any:
        return read_json_with_retries(
            self._transport,
            "GET",
            self._url(path),
            headers=self._headers(),
            timeout=self._timeout,
            retry_policy=self._read_retry_policy,
            sleeper=self._sleeper,
            operation=operation,
        )

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Goog-Api-Key": self._api_key}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"


def _session_name(session: str) -> str:
    value = str(session or "").strip().strip("/")
    if value.startswith("sessions/"):
        session_id = value.split("/", 1)[1]
    else:
        session_id = value
    if not session_id or "/" in session_id:
        raise ValueError("session must be a Jules session id or sessions/{id}")
    return f"sessions/{quote(session_id, safe='')}"


def _activity_identity(activity: Mapping[str, Any]) -> str:
    return str(activity.get("name") or activity.get("id") or "")


def _delivery_receipt(session_name: str, recovery: Mapping[str, Any], *, ambiguous: bool) -> dict[str, Any]:
    return {
        "schema_version": "0.4",
        "provider": "JULES",
        "operation": "sendMessage",
        "session": session_name,
        "outcome": "DELIVERED_AFTER_AMBIGUOUS_WRITE" if ambiguous else "DELIVERED",
        "activity": recovery.get("matched_activity"),
        "safe_to_blind_retry": False,
    }
