from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from typing import Any, Callable, Mapping, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .github_refs import (
    BACKEND_SCHEMA,
    STATE_PATH,
    GitHubGitDataTransport,
    GitHubRefConflict,
    GitHubRefStateStore,
    GitHubRefTransportError,
    GitHubRefWriteUncertain,
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


def _repository_from_remote(value: str) -> str:
    """Extract owner/name from a GitHub checkout remote without exposing credentials."""

    text = str(value or "").strip().rstrip("/")
    if not text:
        raise GitHubRefTransportError("Git checkout remote identity is unavailable")
    text = re.sub(r"\.git$", "", text, flags=re.IGNORECASE)
    patterns = (
        r"^https?://github\.com/([^/]+/[^/]+)$",
        r"^ssh://git@github\.com/([^/]+/[^/]+)$",
        r"^git@github\.com:([^/]+/[^/]+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return _repository(match.group(1))
    raise GitHubRefTransportError("Git checkout remote is not an exact supported GitHub repository")


class OwnerAuthorizedSameRepoGitDataTransport(GitHubGitDataTransport):
    """Explicit owner-authorized transport for runtime state in the UES repository itself.

    The normal GitHubGitDataTransport remains private-repository-only. This class is
    a deliberately separate policy boundary: it permits a public repository only
    when the runtime repository and the exact owner-authorized repository identity
    are identical. Tokens remain runtime-only and state snapshots still pass through
    the normal StateStore receipt sanitizer.

    In GitHub Actions, the trusted same-repository checkout is also used as a
    Git-native StateStore transport. This removes GitHub REST/Git-Data rate-limit
    budget from the runtime-state critical path while preserving exact repository
    identity and non-force fast-forward CAS semantics. Outside GitHub Actions the
    existing REST transport remains the default.

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

    @property
    def git_native_enabled(self) -> bool:
        """Use the authenticated checkout only inside GitHub Actions unless disabled."""

        actions = str(os.environ.get("GITHUB_ACTIONS") or "").strip().lower() == "true"
        disabled = (
            str(os.environ.get("UES_DISABLE_GIT_NATIVE_SAME_REPO") or "").strip().lower()
            == "true"
        )
        return actions and not disabled

    def _git(
        self,
        *args: str,
        input_text: str | None = None,
        remote_write: bool = False,
    ) -> str:
        """Run Git without placing the runtime token on the command line.

        actions/checkout persists repository-scoped authentication in local Git
        configuration. We deliberately reuse that authenticated channel. A failed
        remote push is never retried here: a definite rejection is a CAS conflict;
        every other failed push is outcome-uncertain and must be reconciled by the
        StateStore's authoritative ref readback path.
        """

        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault("GIT_AUTHOR_NAME", "ues-runtime")
        env.setdefault("GIT_AUTHOR_EMAIL", "ues-runtime@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", "ues-runtime")
        env.setdefault("GIT_COMMITTER_EMAIL", "ues-runtime@users.noreply.github.com")
        try:
            completed = subprocess.run(
                ["git", *args],
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            if remote_write:
                raise GitHubRefWriteUncertain("Git push outcome uncertain") from None
            raise GitHubRefTransportError("Git transport unavailable") from None

        if completed.returncode == 0:
            return str(completed.stdout or "")

        diagnostic = f"{completed.stdout or ''}\n{completed.stderr or ''}".casefold()
        if remote_write:
            definite_conflict = any(
                marker in diagnostic
                for marker in (
                    "non-fast-forward",
                    "fetch first",
                    "stale info",
                    "[rejected]",
                    "cannot lock ref",
                )
            )
            if definite_conflict:
                raise GitHubRefConflict("Git ref CAS conflict")
            raise GitHubRefWriteUncertain("Git push outcome uncertain")
        raise GitHubRefTransportError("Git transport read/object operation unavailable")

    def _retry_throttled_read(self, action: Callable[[], _T]) -> _T:
        """Boundedly retry only read-side 403/429 REST transport failures."""

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
        if self.git_native_enabled:
            observed = _repository_from_remote(self._git("remote", "get-url", "origin"))
            if observed.casefold() != self.expected_repository.casefold():
                raise GitHubRefTransportError(
                    "runtime repository identity does not match the owner-authorized repository"
                )
            self.storage_visibility = "OWNER_AUTHORIZED_SAME_REPO"
            self._storage_policy_verified = True
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
        if self.git_native_enabled:
            ref = _required(ref, "ref")
            output = self._git("ls-remote", "--refs", "origin", f"refs/{ref}")
            rows = [line.split() for line in output.splitlines() if line.strip()]
            exact = [row for row in rows if len(row) >= 2 and row[1] == f"refs/{ref}"]
            if not exact:
                return None
            if len(exact) != 1:
                raise GitHubRefTransportError("Git ref read returned ambiguous identity")
            return str(exact[0][0])
        base = super()
        return self._retry_throttled_read(lambda: base.get_ref(ref))

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]:
        if self.git_native_enabled:
            commit_sha = _required(commit_sha, "commit_sha")
            self._git("fetch", "--no-tags", "--depth=1", "origin", commit_sha)
            raw = self._git("show", f"{commit_sha}:{STATE_PATH}")
            try:
                value: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GitHubRefTransportError("state blob is corrupt") from exc
            if not isinstance(value, Mapping):
                raise GitHubRefTransportError("state snapshot must be an object")
            return value
        base = super()
        return self._retry_throttled_read(lambda: base.read_snapshot(commit_sha))

    def create_snapshot_commit(
        self,
        *,
        parent_sha: str | None,
        snapshot: Mapping[str, Any],
        message: str,
    ) -> str:
        if not self.git_native_enabled:
            return super().create_snapshot_commit(
                parent_sha=parent_sha,
                snapshot=snapshot,
                message=message,
            )

        if parent_sha:
            self._git("fetch", "--no-tags", "--depth=1", "origin", _required(parent_sha, "parent_sha"))
        raw = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n"
        blob_sha = self._git("hash-object", "-w", "--stdin", input_text=raw).strip()
        if not blob_sha:
            raise GitHubRefTransportError("Git blob creation returned no SHA")
        tree_input = f"100644 blob {blob_sha}\t{STATE_PATH}\n"
        tree_sha = self._git("mktree", input_text=tree_input).strip()
        if not tree_sha:
            raise GitHubRefTransportError("Git tree creation returned no SHA")
        args = ["commit-tree", tree_sha]
        if parent_sha:
            args.extend(["-p", parent_sha])
        args.extend(["-m", _required(message, "message")])
        commit_sha = self._git(*args).strip()
        if not commit_sha:
            raise GitHubRefTransportError("Git commit creation returned no SHA")
        return commit_sha

    def create_ref(self, ref: str, commit_sha: str) -> None:
        if self.git_native_enabled:
            ref = _required(ref, "ref")
            commit_sha = _required(commit_sha, "commit_sha")
            self._git("push", "origin", f"{commit_sha}:refs/{ref}", remote_write=True)
            return
        super().create_ref(ref, commit_sha)

    def update_ref(self, ref: str, commit_sha: str) -> None:
        if self.git_native_enabled:
            ref = _required(ref, "ref")
            commit_sha = _required(commit_sha, "commit_sha")
            self._git("push", "origin", f"{commit_sha}:refs/{ref}", remote_write=True)
            return
        super().update_ref(ref, commit_sha)

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
        if self.git_native_enabled:
            output = self._git(
                "ls-remote",
                "--refs",
                "origin",
                f"refs/{prefix}*",
            )
            result: dict[str, str] = {}
            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 2 or not parts[1].startswith("refs/"):
                    continue
                result[parts[1][len("refs/"):]] = parts[0]
            return dict(sorted(result.items()))
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
        """Boundedly re-read an already confirmed CAS without repeating its mutation."""

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
        """Boundedly reconcile post-write ref visibility without retrying the write."""

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
