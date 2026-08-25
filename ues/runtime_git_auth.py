from __future__ import annotations

import base64
import os
import re
import subprocess
from typing import MutableMapping

_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?$", re.IGNORECASE)


def _remote_repository() -> str | None:
    try:
        value = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = _REMOTE.fullmatch(value.rstrip("/"))
    return match.group(1) if match else None


def configure_same_repo_git_auth(env: MutableMapping[str, str] | None = None) -> bool:
    """Inject same-repository Git HTTP auth into child Git processes via environment.

    Parent Controller runtime checkouts intentionally use `persist-credentials:false`,
    while the owner-authorized same-repo StateStore uses Git-native non-force pushes.
    This helper restores only the already-granted job token for the exact current
    repository, without writing credentials to `.git/config` or command arguments.
    """

    target = os.environ if env is None else env
    if str(target.get("GITHUB_ACTIONS") or "").lower() != "true":
        return False
    if str(target.get("UES_ALLOW_PUBLIC_SAME_REPO_STATE") or "").lower() != "true":
        return False
    repository = str(target.get("GITHUB_REPOSITORY") or "").strip()
    token = str(target.get("GITHUB_TOKEN") or "")
    if not repository or not token:
        return False
    observed = _remote_repository()
    if observed is None or observed.casefold() != repository.casefold():
        raise RuntimeError("same-repo Git auth requires exact checkout repository identity")

    raw_count = str(target.get("GIT_CONFIG_COUNT") or "0").strip()
    try:
        count = int(raw_count)
    except ValueError as exc:
        raise RuntimeError("invalid inherited GIT_CONFIG_COUNT") from exc
    if count < 0 or count > 128:
        raise RuntimeError("invalid inherited GIT_CONFIG_COUNT")

    key = "http.https://github.com/.extraheader"
    for index in range(count):
        if target.get(f"GIT_CONFIG_KEY_{index}") == key:
            return True

    credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    target[f"GIT_CONFIG_KEY_{count}"] = key
    target[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {credential}"
    target["GIT_CONFIG_COUNT"] = str(count + 1)
    return True
