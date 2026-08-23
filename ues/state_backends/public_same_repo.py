from __future__ import annotations

import json
import socket
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .github_refs import (
    BACKEND_SCHEMA,
    GitHubGitDataTransport,
    GitHubRefStateStore,
    GitHubRefTransportError,
)
from ..state_store import SCHEMA_VERSION, StateStoreCapabilities, StateUnavailable

OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY = "OWNER_AUTHORIZED_PUBLIC_SAME_REPOSITORY"


def _repository(value: str) -> str:
    text = str(value or "").strip().strip("/")
    if text.count("/") != 1 or any(not part for part in text.split("/")):
        raise ValueError("repository must be owner/name")
    return text


class OwnerAuthorizedSameRepoGitDataTransport(GitHubGitDataTransport):
    """Explicit owner-authorized transport for runtime state in the UES repository itself.

    The normal GitHubGitDataTransport remains private-repository-only. This class is
    a deliberately separate policy boundary: it permits a public repository only
    when the runtime repository and the exact owner-authorized repository identity
    are identical. Tokens remain runtime-only and state snapshots still pass through
    the normal StateStore receipt sanitizer.

    Public mode protects secrets, not metadata confidentiality. Lane/session/source
    identifiers persisted by callers may be visible through public runtime refs.
    """

    storage_policy = OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        expected_repository: str,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
    ) -> None:
        repository = _repository(repository)
        expected_repository = _repository(expected_repository)
        if repository.casefold() != expected_repository.casefold():
            raise ValueError("same-repository runtime state requires exact repository identity")
        self.expected_repository = expected_repository
        self.storage_visibility = "UNVERIFIED"
        super().__init__(
            repository,
            token,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
        )

    def assert_private_repository(self) -> None:
        """Verify exact repository identity; public visibility is owner-authorized here only."""

        if self._storage_policy_verified:
            return
        value = self._request_json("GET", self._repo_path)
        assert value is not None
        observed = str(value.get("full_name") or "").strip()
        if not observed or observed.casefold() != self.expected_repository.casefold():
            raise GitHubRefTransportError(
                "runtime repository identity does not match the owner-authorized repository"
            )
        self.storage_visibility = "PRIVATE" if bool(value.get("private")) else "PUBLIC"
        self._storage_policy_verified = True

    def list_refs(self, prefix: str) -> dict[str, str]:
        """List refs under a bounded prefix for restart/watchdog discovery."""

        prefix = str(prefix or "").strip().strip("/")
        if not prefix:
            raise ValueError("ref prefix is required")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "ues-same-repo-state-store",
        }
        request = Request(
            f"{self.api_url}{self._repo_path}/git/matching-refs/{quote(prefix, safe='/')}",
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raise GitHubRefTransportError(
                f"GitHub matching-ref read failed (HTTP {exc.code})"
            ) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise GitHubRefTransportError("GitHub matching-ref read unavailable") from None
        try:
            value: Any = json.loads(raw.decode("utf-8")) if raw else []
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubRefTransportError("GitHub matching-ref response is invalid JSON") from exc
        if not isinstance(value, list):
            raise GitHubRefTransportError("GitHub matching-ref response has invalid shape")

        result: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise GitHubRefTransportError("GitHub matching-ref item has invalid shape")
            ref = str(item.get("ref") or "")
            obj = item.get("object")
            if not ref.startswith("refs/") or not isinstance(obj, Mapping) or not obj.get("sha"):
                raise GitHubRefTransportError("GitHub matching-ref item is missing ref identity")
            result[ref[len("refs/"):]] = str(obj["sha"])
        return dict(sorted(result.items()))


class OwnerAuthorizedSameRepoStateStore(GitHubRefStateStore):
    """Git-ref CAS StateStore with explicit same-public-repository owner authority."""

    transport: OwnerAuthorizedSameRepoGitDataTransport

    @property
    def capabilities(self) -> StateStoreCapabilities:
        return StateStoreCapabilities(
            backend_name="github-owner-authorized-same-repo-ref-cas-v1",
            survives_runner_replacement=True,
            atomic_compare_and_swap=True,
            versioned_state=True,
            lane_local_leases=True,
            durable_operation_records=True,
            authoritative_restart_reconciliation=True,
            conflict_detection=True,
        )

    def _discover_identities(self, kind: str) -> tuple[str, ...]:
        if kind not in {"lane", "operation"}:
            raise ValueError("unsupported state ref kind")
        try:
            refs = self.transport.list_refs(f"heads/{self.ref_prefix}/{kind}/")
        except GitHubRefTransportError as exc:
            raise StateUnavailable(f"runtime state discovery unavailable: {exc}") from exc

        identities: list[str] = []
        for ref, commit_sha in refs.items():
            try:
                snapshot = self.transport.read_snapshot(commit_sha)
                identity = str(snapshot.get("identity") or "")
                version = int(snapshot.get("version") or 0)
                record = snapshot.get("record")
                if snapshot.get("backend_schema") != BACKEND_SCHEMA:
                    raise ValueError("backend schema mismatch")
                if snapshot.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("runtime schema mismatch")
                if snapshot.get("kind") != kind:
                    raise ValueError("state kind mismatch")
                if not identity:
                    raise ValueError("state identity missing")
                if version <= 0:
                    raise ValueError("state version invalid")
                if not isinstance(record, Mapping):
                    raise ValueError("state record invalid")
                if ref != self._ref(kind, identity):
                    raise ValueError("state ref does not match embedded identity")
            except (GitHubRefTransportError, TypeError, ValueError) as exc:
                raise StateUnavailable(f"runtime state discovery found corrupt state: {exc}") from exc
            identities.append(identity)
        return tuple(sorted(set(identities)))

    def discover_lane_ids(self) -> tuple[str, ...]:
        return self._discover_identities("lane")

    def discover_operation_keys(self) -> tuple[str, ...]:
        return self._discover_identities("operation")
