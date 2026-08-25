from __future__ import annotations

import os

from .github_refs import GitHubRefTransportError
from .public_same_repo import (
    OwnerAuthorizedSameRepoGitDataTransport,
    OwnerAuthorizedSameRepoStateStore,
)


class RecoverySameRepoGitDataTransport(OwnerAuthorizedSameRepoGitDataTransport):
    """Same-repository transport with a bounded Git-data identity fallback.

    The ordinary owner-authorized transport verifies repository identity through
    repository metadata before touching runtime refs. GitHub Actions has repeatedly
    returned transient/permission-shaped 403 responses on that metadata GET while
    contents/Git-data access remains available. Terminal recovery may therefore use
    an exact-repository Git-data read as a second proof path. Both paths address the
    exact owner/name supplied by trusted runtime context; no write is attempted by
    this preflight and persistent failure remains fail-closed.
    """

    def assert_private_repository(self) -> None:
        if self._storage_policy_verified:
            return
        try:
            super().assert_private_repository()
            return
        except GitHubRefTransportError as metadata_error:
            try:
                # A successful matching-ref read proves authenticated Git-data read
                # access to the exact repository path. Empty results are valid.
                self._retry_throttled_read(
                    lambda: self._list_refs_once("heads/ues-runtime/")
                )
            except GitHubRefTransportError as git_data_error:
                raise GitHubRefTransportError(
                    "same-repository StateStore identity proof unavailable via metadata and Git-data reads"
                ) from git_data_error
            self.storage_visibility = "OWNER_AUTHORIZED_SAME_REPOSITORY_CONTEXT"
            self._storage_policy_verified = True


def build_recovery_state_store() -> OwnerAuthorizedSameRepoStateStore:
    """Build the canonical StateStore with the bounded recovery read preflight."""

    if str(os.environ.get("UES_ALLOW_PUBLIC_SAME_REPO_STATE") or "").strip().lower() != "true":
        raise RuntimeError(
            "UES_ALLOW_PUBLIC_SAME_REPO_STATE=true is required for the explicit owner-authorized policy"
        )
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if repository.count("/") != 1:
        raise RuntimeError("GITHUB_REPOSITORY is required for terminal recovery StateStore")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for terminal recovery StateStore")
    prefix = str(os.environ.get("UES_STATE_REF_PREFIX") or "ues-runtime/v2").strip()
    transport = RecoverySameRepoGitDataTransport(
        repository,
        token,
        expected_repository=repository,
    )
    return OwnerAuthorizedSameRepoStateStore(transport, ref_prefix=prefix)
