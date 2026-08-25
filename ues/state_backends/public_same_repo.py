from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Mapping, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .github_refs import (
    BACKEND_SCHEMA,
    GitHubGitDataTransport,
    GitHubRefStateStore,
    GitHubRefTransportError,
    _iso,
    _required,
)
from ..state_store import (
    SCHEMA_VERSION,
    OperationRead,
    OperationRecord,
    StateRead,
    StateStoreCapabilities,
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
)

OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY = "OWNER_AUTHORIZED_PUBLIC_SAME_REPOSITORY"
_T = TypeVar("_T")


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
    read_throttle_attempts = 3
    read_throttle_delay_seconds = 0.75

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

    def _retry_throttled_read(self, action: Callable[[], _T]) -> _T:
        """Boundedly retry only read-side 403/429 transport failures.

        Runtime state uses GitHub Git Data reads heavily. A concurrent observation
        burst can receive a temporary 403/429 even though the token remains valid.
        Repeating GET-only work is side-effect free, so this transport gives those
        statuses a short bounded recovery window. Writes are intentionally not
        wrapped here and therefore preserve the existing no-blind-write-retry rule.

        A persistent or authorization-related 403 still fails closed after the
        bounded read attempts; no mutation is attempted as part of recovery.
        """

        attempts = max(1, int(self.read_throttle_attempts))
        delay = max(0.0, float(self.read_throttle_delay_seconds))
        for attempt in range(attempts):
            try:
                return action()
            except GitHubRefTransportError as exc:
                message = str(exc)
                throttled = "(HTTP 403)" in message or "(HTTP 429)" in message
                if not throttled or attempt + 1 >= attempts:
                    raise
                if delay:
                    time.sleep(delay * (attempt + 1))
        raise AssertionError("unreachable read retry state")

    def assert_private_repository(self) -> None:
        """Verify exact repository identity; public visibility is owner-authorized here only."""

        if self._storage_policy_verified:
            return
        value = self._retry_throttled_read(lambda: self._request_json("GET", self._repo_path))
        assert value is not None
        observed = str(value.get("full_name") or "").strip()
        if not observed or observed.casefold() != self.expected_repository.casefold():
            raise GitHubRefTransportError(
                "runtime repository identity does not match the owner-authorized repository"
            )
        self.storage_visibility = "PRIVATE" if bool(value.get("private")) else "PUBLIC"
        self._storage_policy_verified = True

    def get_ref(self, ref: str) -> str | None:
        base = super()
        return self._retry_throttled_read(lambda: base.get_ref(ref))

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]:
        base = super()
        return self._retry_throttled_read(lambda: base.read_snapshot(commit_sha))

    def _list_refs_once(self, prefix: str) -> dict[str, str]:
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

    def list_refs(self, prefix: str) -> dict[str, str]:
        """List refs under a bounded prefix for restart/watchdog discovery."""

        prefix = str(prefix or "").strip().strip("/")
        if not prefix:
            raise ValueError("ref prefix is required")
        return self._retry_throttled_read(lambda: self._list_refs_once(prefix))


class OwnerAuthorizedSameRepoStateStore(GitHubRefStateStore):
    """Git-ref CAS StateStore with explicit same-public-repository owner authority."""

    transport: OwnerAuthorizedSameRepoGitDataTransport
    publish_readback_attempts = 7
    publish_readback_delay_seconds = 0.5
    post_cas_readback_attempts = 3
    post_cas_readback_delay_seconds = 0.5

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

    def _retry_post_cas_read(self, action: Callable[[], _T], *, unavailable_message: str) -> _T:
        """Boundedly re-read an already confirmed CAS without repeating its mutation.

        `_cas` returns only after the proposed ref is authoritatively reconciled. A
        following lane/operation materialization read can still hit a short read-side
        outage. Retrying only that read preserves the one-write rule while preventing
        a transient GET failure from turning a confirmed CAS into a false failure.
        Persistent unavailability and any non-UNAVAILABLE state still fail closed.
        """

        attempts = max(1, int(self.post_cas_readback_attempts))
        delay = max(0.0, float(self.post_cas_readback_delay_seconds))
        last_unavailable: StateUnavailable | None = None
        for attempt in range(attempts):
            try:
                read = action()
            except StateUnavailable as exc:
                last_unavailable = exc
            else:
                status = str(getattr(read, "status", "") or "")
                if status == "OK":
                    return read
                reason = str(getattr(read, "reason", "") or unavailable_message)
                if status != "UNAVAILABLE":
                    raise StateUnavailable(reason)
                last_unavailable = StateUnavailable(reason)
            if attempt + 1 < attempts and delay:
                time.sleep(delay)

        if last_unavailable is not None:
            raise StateUnavailable(unavailable_message) from last_unavailable
        raise StateUnavailable(unavailable_message)

    def compare_and_swap_workstream(
        self,
        lane_id: str,
        expected_version: int,
        record: WorkstreamRuntimeRecord,
    ) -> StateRead:
        lane_id = _required(lane_id, "lane_id")
        if record.lane_id != lane_id:
            raise ValueError("lane identity mismatch")
        record.updated_at = _iso(self.clock())
        self._cas("lane", lane_id, expected_version, record.to_dict())
        return self._retry_post_cas_read(
            lambda: self.read_workstream(lane_id),
            unavailable_message="lane state unavailable after CAS",
        )

    def compare_and_swap_operation(
        self,
        operation_key: str,
        expected_version: int,
        record: OperationRecord,
    ) -> OperationRead:
        operation_key = _required(operation_key, "operation_key")
        if record.operation_key != operation_key:
            raise ValueError("operation identity mismatch")
        self._cas("operation", operation_key, expected_version, record.to_dict())
        return self._retry_post_cas_read(
            lambda: self.read_operation(operation_key),
            unavailable_message="operation state unavailable after CAS",
        )

    def _resolve_publish(
        self,
        *,
        ref: str,
        previous_sha: str | None,
        proposed_sha: str,
        conflict: bool,
    ) -> None:
        """Boundedly reconcile post-write ref visibility without retrying the write.

        GitHub may acknowledge a non-force ref update before a subsequent GET exposes
        the new object. A single stale or temporarily unavailable read must therefore
        not convert a committed StateStore CAS into a false failure. Definite conflicts
        still fail immediately; normal/uncertain writes only re-read the authoritative
        ref for a bounded period. No create/update mutation is repeated here.
        """

        if conflict:
            super()._resolve_publish(
                ref=ref,
                previous_sha=previous_sha,
                proposed_sha=proposed_sha,
                conflict=True,
            )
            return

        attempts = max(1, int(self.publish_readback_attempts))
        delay = max(0.0, float(self.publish_readback_delay_seconds))
        for attempt in range(attempts):
            try:
                observed = self.transport.get_ref(ref)
            except GitHubRefTransportError as exc:
                if attempt + 1 >= attempts:
                    raise StateUnavailable(
                        "state write outcome requires authoritative ref readback"
                    ) from exc
                if delay:
                    time.sleep(delay)
                continue
            if observed == proposed_sha:
                return
            if observed != previous_sha:
                raise StateUnavailable(
                    "state ref diverged after write attempt; authoritative reconciliation required"
                )
            if attempt + 1 < attempts and delay:
                time.sleep(delay)

        raise StateVersionConflict(
            "state write was not observed after bounded authoritative readback; no overwrite performed"
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
