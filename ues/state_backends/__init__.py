"""Production-capable StateStore backends.

Backends in this package implement storage mechanics only. They do not grant
project authority, canary authority, provider mutation authority, merge
authority, or activation authority.
"""

from .github_refs import (
    GitHubGitDataTransport,
    GitHubRefConflict,
    GitHubRefStateStore,
    GitHubRefTransport,
    GitHubRefTransportError,
    GitHubRefWriteUncertain,
)

__all__ = [
    "GitHubGitDataTransport",
    "GitHubRefConflict",
    "GitHubRefStateStore",
    "GitHubRefTransport",
    "GitHubRefTransportError",
    "GitHubRefWriteUncertain",
]
