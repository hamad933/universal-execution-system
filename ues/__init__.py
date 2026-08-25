"""Universal Execution System reference implementation."""

from .runtime_git_auth import configure_same_repo_git_auth

__version__ = "0.0.1"

# Parent Controller effect jobs intentionally checkout trusted runtime without
# persisted credentials. The explicit owner-authorized same-repository StateStore
# still needs its already-granted job token for Git-native non-force CAS pushes.
# This is a no-op outside that exact runtime mode and writes no credential to disk.
configure_same_repo_git_auth()
