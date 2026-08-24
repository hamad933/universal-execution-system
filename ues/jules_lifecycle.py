from __future__ import annotations

from typing import Any

from .providers.base import NetworkError, ProtocolError, WriteOutcomeUnknown, encode_json, error_for_response
from .providers.jules import JulesClient, _resource_name


class JulesLifecycleClient(JulesClient):
    """Jules client extension for explicit same-lineage session generation creation.

    Creation is a distinct external effect. Callers must perform durable
    idempotency/authority/budget checks before invoking this method.
    """

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        source: str,
        starting_branch: str,
        require_plan_approval: bool = False,
        automation_mode: str = "AUTO_CREATE_PR",
        expected_repository: str,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt is required")
        if not title.strip():
            raise ValueError("title is required")
        if not starting_branch.strip():
            raise ValueError("starting_branch is required")
        if not expected_repository.strip():
            raise ValueError("expected_repository is required")

        source_name = _resource_name(source, "sources", allow_nested=True)
        source_binding = self.get_source(source_name)
        repository = str(source_binding.get("repository") or "")
        if not source_binding.get("explicitRepositoryIdentity") or repository.casefold() != expected_repository.casefold():
            raise ProtocolError(
                "Jules source repository does not match expected repository",
                operation="jules.sessions.create",
            )

        body = {
            "prompt": prompt,
            "title": title,
            "sourceContext": {
                "source": source_name,
                "githubRepoContext": {"startingBranch": starting_branch},
            },
            "requirePlanApproval": bool(require_plan_approval),
            "automationMode": automation_mode,
        }
        try:
            response = self._transport.request(
                "POST",
                self._url("/v1alpha/sessions"),
                headers=self._headers(json_body=True),
                body=encode_json(body),
                timeout=self._timeout,
            )
        except NetworkError as exc:
            raise WriteOutcomeUnknown(
                "Jules create-session transport result is unknown",
                operation="jules.sessions.create",
                recovery={"verdict": "AUTHORITATIVE_SESSION_ENUMERATION_REQUIRED", "safe_to_blind_retry": False},
            ) from exc

        if not 200 <= response.status <= 299:
            raise error_for_response(response, operation="jules.sessions.create")

        try:
            import json

            payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise WriteOutcomeUnknown(
                "Jules create-session returned unreadable success payload",
                operation="jules.sessions.create",
                recovery={"verdict": "AUTHORITATIVE_SESSION_ENUMERATION_REQUIRED", "safe_to_blind_retry": False},
            ) from exc
        if not isinstance(payload, dict):
            raise WriteOutcomeUnknown(
                "Jules create-session success payload was not an object",
                operation="jules.sessions.create",
                recovery={"verdict": "AUTHORITATIVE_SESSION_ENUMERATION_REQUIRED", "safe_to_blind_retry": False},
            )

        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise WriteOutcomeUnknown(
                "Jules create-session success payload omitted session identity",
                operation="jules.sessions.create",
                recovery={"verdict": "AUTHORITATIVE_SESSION_ENUMERATION_REQUIRED", "safe_to_blind_retry": False},
            )

        readback = self.get_session(name)
        if str(readback.get("sourceIdentifier") or "") != source_name:
            raise WriteOutcomeUnknown(
                "Jules created session source readback mismatch",
                operation="jules.sessions.create",
                recovery={"verdict": "CREATED_SESSION_BINDING_MISMATCH", "safe_to_blind_retry": False},
            )
        if str(readback.get("sourceStartingBranch") or "") != starting_branch:
            raise WriteOutcomeUnknown(
                "Jules created session branch readback mismatch",
                operation="jules.sessions.create",
                recovery={"verdict": "CREATED_SESSION_BINDING_MISMATCH", "safe_to_blind_retry": False},
            )
        binding = self.get_session_source_binding(name, expected_repository=expected_repository)
        if not binding.get("proven") or not binding.get("matches_expected_repository"):
            raise WriteOutcomeUnknown(
                "Jules created session repository binding was not proven",
                operation="jules.sessions.create",
                recovery={"verdict": "CREATED_SESSION_BINDING_UNPROVEN", "safe_to_blind_retry": False},
            )
        return {
            "provider": "JULES",
            "operation": "createSession",
            "session": name,
            "state": readback.get("normalizedState"),
            "source": source_name,
            "repository": repository,
            "starting_branch": starting_branch,
            "authoritative_readback": True,
            "safe_to_blind_retry": False,
        }


def terminal_session_continuation_supported(state: str) -> bool:
    """Jules sendMessage is documented for active sessions, not terminal states."""

    return str(state or "").upper() not in {"COMPLETED", "FAILED"}
