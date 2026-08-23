from __future__ import annotations

import base64
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..operation_records import sanitize_receipt
from ..state_store import (
    SCHEMA_VERSION,
    SHADOW_MODE,
    Lease,
    LeaseAcquireResult,
    LeaseCollision,
    OperationRead,
    OperationRecord,
    StateRead,
    StateStore,
    StateStoreCapabilities,
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
)

BACKEND_SCHEMA = "ues-github-ref-state-v1"
STATE_PATH = "state.json"
DEFAULT_REF_PREFIX = "ues-runtime/v1"
_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9._/-]+$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


class GitHubRefTransportError(RuntimeError):
    pass


class GitHubRefConflict(GitHubRefTransportError):
    """Definite non-fast-forward or create-ref collision."""


class GitHubRefWriteUncertain(GitHubRefTransportError):
    """A write request may have reached GitHub; authoritative ref readback is required."""


class GitHubRefTransport(Protocol):
    repository: str

    def assert_private_repository(self) -> None: ...

    def get_ref(self, ref: str) -> str | None: ...

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]: ...

    def create_snapshot_commit(
        self,
        *,
        parent_sha: str | None,
        snapshot: Mapping[str, Any],
        message: str,
    ) -> str: ...

    def create_ref(self, ref: str, commit_sha: str) -> None: ...

    def update_ref(self, ref: str, commit_sha: str) -> None: ...


class GitHubGitDataTransport:
    """Minimal Git Data API transport for a dedicated private runtime-state repository.

    The token exists only in process memory and is never included in persisted state,
    exceptions, repr output, commit messages, or request bodies other than the
    Authorization header.
    """

    api_version = "2022-11-28"

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.repository = _required(repository, "repository")
        self._token = _required(token, "token")
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._storage_policy_verified = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(repository={self.repository!r}, "
            f"api_url={self.api_url!r}, token='[REDACTED]')"
        )

    @property
    def _repo_path(self) -> str:
        owner, sep, name = self.repository.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError("repository must be owner/name")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        write: bool = False,
        allow_404: bool = False,
    ) -> Mapping[str, Any] | None:
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "ues-github-ref-state-store",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            if write and exc.code in {409, 422}:
                raise GitHubRefConflict(f"GitHub ref CAS conflict (HTTP {exc.code})") from None
            if write and (exc.code == 429 or exc.code >= 500):
                raise GitHubRefWriteUncertain(
                    f"GitHub write outcome uncertain (HTTP {exc.code})"
                ) from None
            raise GitHubRefTransportError(f"GitHub API request failed (HTTP {exc.code})") from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            if write:
                raise GitHubRefWriteUncertain("GitHub write outcome uncertain") from None
            raise GitHubRefTransportError("GitHub API read unavailable") from None
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubRefTransportError("GitHub API returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise GitHubRefTransportError("GitHub API returned an unexpected JSON shape")
        return value

    def assert_private_repository(self) -> None:
        if self._storage_policy_verified:
            return
        value = self._request_json("GET", self._repo_path)
        assert value is not None
        if not bool(value.get("private")):
            raise GitHubRefTransportError(
                "runtime state repository must be private; public repository rejected"
            )
        self._storage_policy_verified = True

    def get_ref(self, ref: str) -> str | None:
        ref = _required(ref, "ref")
        value = self._request_json(
            "GET",
            f"{self._repo_path}/git/ref/{quote(ref, safe='/')}",
            allow_404=True,
        )
        if value is None:
            return None
        obj = value.get("object")
        if not isinstance(obj, Mapping) or not obj.get("sha"):
            raise GitHubRefTransportError("GitHub ref response is missing object SHA")
        return str(obj["sha"])

    def _get_commit(self, sha: str) -> Mapping[str, Any]:
        value = self._request_json(
            "GET", f"{self._repo_path}/git/commits/{quote(_required(sha, 'sha'), safe='')}"
        )
        assert value is not None
        return value

    def read_snapshot(self, commit_sha: str) -> Mapping[str, Any]:
        commit = self._get_commit(commit_sha)
        tree = commit.get("tree")
        if not isinstance(tree, Mapping) or not tree.get("sha"):
            raise GitHubRefTransportError("state commit is missing tree identity")
        tree_value = self._request_json(
            "GET",
            f"{self._repo_path}/git/trees/{quote(str(tree['sha']), safe='')}",
        )
        assert tree_value is not None
        entries = tree_value.get("tree")
        if not isinstance(entries, list):
            raise GitHubRefTransportError("state tree has invalid shape")
        matches = [
            item
            for item in entries
            if isinstance(item, Mapping)
            and item.get("path") == STATE_PATH
            and item.get("type") == "blob"
            and item.get("sha")
        ]
        if len(matches) != 1:
            raise GitHubRefTransportError("state commit must contain exactly one state.json blob")
        blob = self._request_json(
            "GET",
            f"{self._repo_path}/git/blobs/{quote(str(matches[0]['sha']), safe='')}",
        )
        assert blob is not None
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise GitHubRefTransportError("state blob must be base64 encoded")
        try:
            raw = base64.b64decode(str(blob["content"]).replace("\n", ""), validate=True)
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubRefTransportError("state blob is corrupt") from exc
        if not isinstance(value, Mapping):
            raise GitHubRefTransportError("state snapshot must be an object")
        return value

    def create_snapshot_commit(
        self,
        *,
        parent_sha: str | None,
        snapshot: Mapping[str, Any],
        message: str,
    ) -> str:
        raw = (
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        blob = self._request_json(
            "POST",
            f"{self._repo_path}/git/blobs",
            payload={"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
            write=True,
        )
        assert blob is not None
        blob_sha = str(blob.get("sha") or "")
        if not blob_sha:
            raise GitHubRefWriteUncertain("GitHub blob creation returned no SHA")

        tree = self._request_json(
            "POST",
            f"{self._repo_path}/git/trees",
            payload={
                "tree": [
                    {
                        "path": STATE_PATH,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                ]
            },
            write=True,
        )
        assert tree is not None
        tree_sha = str(tree.get("sha") or "")
        if not tree_sha:
            raise GitHubRefWriteUncertain("GitHub tree creation returned no SHA")

        parents = [parent_sha] if parent_sha else []
        commit = self._request_json(
            "POST",
            f"{self._repo_path}/git/commits",
            payload={"message": message, "tree": tree_sha, "parents": parents},
            write=True,
        )
        assert commit is not None
        commit_sha = str(commit.get("sha") or "")
        if not commit_sha:
            raise GitHubRefWriteUncertain("GitHub commit creation returned no SHA")
        return commit_sha

    def create_ref(self, ref: str, commit_sha: str) -> None:
        self._request_json(
            "POST",
            f"{self._repo_path}/git/refs",
            payload={"ref": f"refs/{_required(ref, 'ref')}", "sha": _required(commit_sha, "commit_sha")},
            write=True,
        )

    def update_ref(self, ref: str, commit_sha: str) -> None:
        self._request_json(
            "PATCH",
            f"{self._repo_path}/git/refs/{quote(_required(ref, 'ref'), safe='/')}",
            payload={"sha": _required(commit_sha, "commit_sha"), "force": False},
            write=True,
        )


@dataclass(frozen=True)
class _LoadedSnapshot:
    ref: str
    commit_sha: str
    version: int
    record: Mapping[str, Any]


class GitHubRefStateStore(StateStore):
    """Cross-run StateStore backed by lane/operation-sharded Git refs.

    Each canonical lane and operation owns an independent ref. A ref update is a
    non-force fast-forward CAS. The state repository must be private. Unknown
    write outcomes are resolved by authoritative ref readback; the backend never
    blindly retries a ref mutation.
    """

    def __init__(
        self,
        transport: GitHubRefTransport,
        *,
        ref_prefix: str = DEFAULT_REF_PREFIX,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport
        self.ref_prefix = self._validate_prefix(ref_prefix)
        self.clock = clock or _utc_now
        self.transport.assert_private_repository()

    @staticmethod
    def _validate_prefix(value: str) -> str:
        prefix = _required(value, "ref_prefix").strip("/")
        if (
            not _SAFE_PREFIX.fullmatch(prefix)
            or ".." in prefix
            or "@{" in prefix
            or prefix.endswith(".")
            or prefix.endswith("/")
        ):
            raise ValueError("ref_prefix is not safe for Git refs")
        return prefix

    @property
    def capabilities(self) -> StateStoreCapabilities:
        return StateStoreCapabilities(
            backend_name="github-private-ref-cas-v1",
            survives_runner_replacement=True,
            atomic_compare_and_swap=True,
            versioned_state=True,
            lane_local_leases=True,
            durable_operation_records=True,
            authoritative_restart_reconciliation=True,
            conflict_detection=True,
        )

    def _ref(self, kind: str, identity: str) -> str:
        if kind not in {"lane", "operation"}:
            raise ValueError("unsupported state ref kind")
        digest = sha256(_required(identity, "identity").encode("utf-8")).hexdigest()
        return f"heads/{self.ref_prefix}/{kind}/{digest}"

    def lane_ref(self, lane_id: str) -> str:
        return self._ref("lane", lane_id)

    def operation_ref(self, operation_key: str) -> str:
        return self._ref("operation", operation_key)

    @staticmethod
    def _shadow(status: str, reason: str) -> StateRead:
        return StateRead(status, 0, None, SHADOW_MODE, False, reason, False)

    def _load(self, kind: str, identity: str) -> _LoadedSnapshot | None:
        ref = self._ref(kind, identity)
        try:
            commit_sha = self.transport.get_ref(ref)
        except GitHubRefTransportError as exc:
            raise StateUnavailable(f"runtime state ref read unavailable: {exc}") from exc
        if commit_sha is None:
            return None
        try:
            snapshot = self.transport.read_snapshot(commit_sha)
        except GitHubRefTransportError as exc:
            raise StateUnavailable(f"runtime state snapshot unavailable: {exc}") from exc
        try:
            if snapshot.get("backend_schema") != BACKEND_SCHEMA:
                raise ValueError("backend schema mismatch")
            if snapshot.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("runtime schema mismatch")
            if snapshot.get("kind") != kind:
                raise ValueError("state kind mismatch")
            if snapshot.get("identity") != identity:
                raise ValueError("state identity mismatch")
            version = int(snapshot["version"])
            if version <= 0:
                raise ValueError("state version must be positive")
            record = snapshot["record"]
            if not isinstance(record, Mapping):
                raise ValueError("record must be an object")
        except (KeyError, TypeError, ValueError) as exc:
            raise StateUnavailable(f"runtime state snapshot is corrupt: {exc}") from exc
        return _LoadedSnapshot(ref, commit_sha, version, record)

    def _resolve_publish(
        self,
        *,
        ref: str,
        previous_sha: str | None,
        proposed_sha: str,
        conflict: bool,
    ) -> None:
        try:
            observed = self.transport.get_ref(ref)
        except GitHubRefTransportError as exc:
            raise StateUnavailable(
                "state write outcome requires authoritative ref readback"
            ) from exc
        if observed == proposed_sha:
            return
        if observed == previous_sha:
            if conflict:
                raise StateVersionConflict("state CAS conflict; no overwrite performed")
            raise StateVersionConflict("state write was not observed; no overwrite performed")
        raise StateUnavailable(
            "state ref diverged after write attempt; authoritative reconciliation required"
        )

    def _cas(
        self,
        kind: str,
        identity: str,
        expected_version: int,
        record: Mapping[str, Any],
    ) -> None:
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        loaded = self._load(kind, identity)
        actual = loaded.version if loaded else 0
        if actual != expected_version:
            raise StateVersionConflict(
                f"{kind} state version {actual} != expected {expected_version}"
            )
        new_version = actual + 1
        snapshot = sanitize_receipt(
            {
                "backend_schema": BACKEND_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "kind": kind,
                "identity": identity,
                "version": new_version,
                "record": dict(record),
            }
        )
        previous_sha = loaded.commit_sha if loaded else None
        message = f"ues(state): {kind} {sha256(identity.encode('utf-8')).hexdigest()[:16]} v{new_version}"
        try:
            proposed_sha = self.transport.create_snapshot_commit(
                parent_sha=previous_sha,
                snapshot=snapshot,
                message=message,
            )
        except GitHubRefWriteUncertain as exc:
            raise StateUnavailable("state object creation outcome uncertain; ref unchanged") from exc
        except GitHubRefTransportError as exc:
            raise StateUnavailable(f"state object creation failed: {exc}") from exc

        try:
            if loaded is None:
                self.transport.create_ref(self._ref(kind, identity), proposed_sha)
            else:
                self.transport.update_ref(self._ref(kind, identity), proposed_sha)
        except GitHubRefConflict:
            self._resolve_publish(
                ref=self._ref(kind, identity),
                previous_sha=previous_sha,
                proposed_sha=proposed_sha,
                conflict=True,
            )
        except GitHubRefWriteUncertain:
            self._resolve_publish(
                ref=self._ref(kind, identity),
                previous_sha=previous_sha,
                proposed_sha=proposed_sha,
                conflict=False,
            )
        except GitHubRefTransportError as exc:
            raise StateUnavailable(f"state ref mutation failed: {exc}") from exc

        self._resolve_publish(
            ref=self._ref(kind, identity),
            previous_sha=previous_sha,
            proposed_sha=proposed_sha,
            conflict=False,
        )

    def read_workstream(self, lane_id: str) -> StateRead:
        lane_id = _required(lane_id, "lane_id")
        try:
            loaded = self._load("lane", lane_id)
        except StateUnavailable as exc:
            return self._shadow("UNAVAILABLE", str(exc))
        if loaded is None:
            return self._shadow("MISSING", "lane runtime state is missing")
        try:
            record = WorkstreamRuntimeRecord.from_dict(loaded.record)
            if record.lane_id != lane_id:
                raise ValueError("lane identity mismatch")
        except (TypeError, ValueError) as exc:
            return self._shadow("CORRUPT", f"lane runtime state is corrupt: {exc}")
        candidate = record.activation_mode in {"CANARY", "ACTIVE_AUTO_SAFE"}
        return StateRead(
            "OK",
            loaded.version,
            record,
            record.activation_mode,
            False,
            None,
            candidate,
        )

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
        read = self.read_workstream(lane_id)
        if read.status != "OK":
            raise StateUnavailable(read.reason or "lane state unavailable after CAS")
        return read

    def read_operation(self, operation_key: str) -> OperationRead:
        operation_key = _required(operation_key, "operation_key")
        # Operation read failure must never be confused with MISSING. The mutation
        # claim path treats MISSING as eligible for a new operation, so transport
        # unavailability propagates as StateUnavailable and fails closed before lease.
        loaded = self._load("operation", operation_key)
        if loaded is None:
            return OperationRead("MISSING", 0, None)
        try:
            record = OperationRecord.from_dict(loaded.record)
            if record.operation_key != operation_key:
                raise ValueError("operation identity mismatch")
        except (TypeError, ValueError) as exc:
            return OperationRead("CORRUPT", 0, None, str(exc))
        return OperationRead("OK", loaded.version, record)

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
        read = self.read_operation(operation_key)
        if read.status != "OK":
            raise StateUnavailable(read.reason or "operation state unavailable after CAS")
        return read

    def acquire_lease(
        self,
        lane_id: str,
        owner: str,
        operation_key: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquireResult:
        lane_id = _required(lane_id, "lane_id")
        owner = _required(owner, "owner")
        operation_key = _required(operation_key, "operation_key")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = (now or self.clock()).astimezone(timezone.utc)
        read = self.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "cannot lease unavailable lane state")
        record = read.record
        stale = False
        if record.lease is not None:
            if current < _parse_time(record.lease.expires_at):
                raise LeaseCollision(f"lane {lane_id} already leased by {record.lease.owner}")
            stale = True
        seed = f"{lane_id}|{owner}|{operation_key}|{_iso(current)}|{read.version + 1}"
        lease = Lease(
            sha256(seed.encode("utf-8")).hexdigest()[:32],
            owner,
            operation_key,
            _iso(current),
            _iso(current + timedelta(seconds=ttl_seconds)),
        )
        record.lease = lease
        record.action_in_flight = {
            "operation_key": operation_key,
            "owner": owner,
            "started_at": _iso(current),
        }
        record.operation_key = operation_key
        record.updated_at = _iso(current)
        try:
            saved = self.compare_and_swap_workstream(lane_id, read.version, record)
        except StateVersionConflict as exc:
            raise LeaseCollision("concurrent lane update prevented lease acquisition") from exc
        return LeaseAcquireResult(lease, saved.version, stale)

    def release_lease(
        self,
        lane_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> StateRead:
        lane_id = _required(lane_id, "lane_id")
        lease_id = _required(lease_id, "lease_id")
        read = self.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lane runtime state unavailable")
        record = read.record
        if record.lease is None:
            return read
        if record.lease.lease_id != lease_id:
            raise LeaseCollision("lease ownership mismatch")
        record.lease = None
        record.action_in_flight = None
        record.updated_at = _iso((now or self.clock()).astimezone(timezone.utc))
        return self.compare_and_swap_workstream(lane_id, read.version, record)
