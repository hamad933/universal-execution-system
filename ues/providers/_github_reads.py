from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote

from .base import ProtocolError

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class GitHubReadMixin:
        def get_repository_identity(self, owner: str, repo: str) -> dict[str, Any]:
            payload = self._read_json(self._repo_path(owner, repo), operation="github.repository.get")
            obj = _object(payload, "repository")
            return {
                "id": obj.get("id"),
                "full_name": obj.get("full_name"),
                "default_branch": obj.get("default_branch"),
                "visibility": obj.get("visibility"),
                "authority_controls_weakened_by_public_visibility": False,
            }

        def get_ref_head(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
            clean_ref = str(ref or "").removeprefix("refs/heads/").removeprefix("heads/")
            if not clean_ref:
                raise ValueError("ref is required")
            payload = self._read_json(
                f"{self._repo_path(owner, repo)}/git/ref/heads/{quote(clean_ref, safe='')}",
                operation="github.ref.get",
            )
            obj = _object(payload, "ref")
            target = obj.get("object")
            if not isinstance(target, Mapping) or not _is_full_sha(target.get("sha")):
                raise ProtocolError("GitHub ref response missing exact SHA", operation="github.ref.get")
            return {"ref": obj.get("ref"), "head_sha": str(target["sha"]), "object_type": target.get("type")}

        def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
            payload = self._read_json(
                f"{self._repo_path(owner, repo)}/pulls/{int(number)}",
                operation="github.pull.get",
            )
            obj = _object(payload, "pull request")
            head = _object(obj.get("head"), "pull request head")
            base = _object(obj.get("base"), "pull request base")
            if not _is_full_sha(head.get("sha")) or not _is_full_sha(base.get("sha")):
                raise ProtocolError("GitHub PR response missing exact head/base SHA", operation="github.pull.get")
            return {
                "number": obj.get("number"),
                "state": obj.get("state"),
                "draft": bool(obj.get("draft")),
                "merged": bool(obj.get("merged")),
                "head_ref": head.get("ref"),
                "head_sha": head.get("sha"),
                "base_ref": base.get("ref"),
                "base_sha": base.get("sha"),
                "merge_commit_sha": obj.get("merge_commit_sha"),
            }

        def get_exact_commit(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
            _require_full_sha(sha)
            payload = self._read_json(
                f"{self._repo_path(owner, repo)}/commits/{sha}",
                operation="github.commit.get",
            )
            obj = _object(payload, "commit")
            actual = str(obj.get("sha") or "")
            if actual.lower() != sha.lower():
                raise ProtocolError("GitHub commit response does not match exact requested SHA", operation="github.commit.get")
            return {"sha": actual, "exact_sha_match": True}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"GitHub {label} response must be an object")
    return value


def _is_full_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_SHA.fullmatch(value))


def _require_full_sha(value: str) -> None:
    if not _is_full_sha(value):
        raise ValueError("exact full 40-character commit SHA is required")
