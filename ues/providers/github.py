from __future__ import annotations

import time
from typing import Callable

from .base import HttpTransport, RetryPolicy, UrllibTransport
from ._github_ci import GitHubCIMixin
from ._github_dispatch import GitHubDispatchMixin
from ._github_evidence import GitHubEvidenceMixin
from ._github_reads import GitHubReadMixin

GITHUB_API_ENDPOINT = "https://api.github.com"


class GitHubClient(GitHubDispatchMixin, GitHubCIMixin, GitHubEvidenceMixin, GitHubReadMixin):
        def __init__(
            self,
            token: str,
            *,
            transport: HttpTransport | None = None,
            endpoint: str = GITHUB_API_ENDPOINT,
            timeout: float = 15.0,
            read_retry_policy: RetryPolicy | None = None,
            sleeper: Callable[[float], None] = time.sleep,
        ) -> None:
            if not token:
                raise ValueError("token is required at runtime")
            self._token = token
            self._transport = transport or UrllibTransport()
            self._endpoint = endpoint.rstrip("/")
            self._timeout = timeout
            self._read_retry_policy = read_retry_policy or RetryPolicy()
            self._sleeper = sleeper

        def __repr__(self) -> str:
            return f"GitHubClient(endpoint={self._endpoint!r}, token=<redacted>)"
