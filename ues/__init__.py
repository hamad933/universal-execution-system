"""Universal Execution System reference implementation."""

from .runtime_git_auth import configure_same_repo_git_auth

__version__ = "0.0.1"

# Parent Controller effect jobs intentionally checkout trusted runtime without
# persisted credentials. The explicit owner-authorized same-repository StateStore
# still needs its already-granted job token for Git-native non-force CAS pushes.
# This is a no-op outside that exact runtime mode and writes no credential to disk.
configure_same_repo_git_auth()

# Evidence supplements use the canonical initial-generation path for generation 0.
# Once a durable predecessor exists, install the bounded same-logical-lineage
# continuation shim so later physical generations use the shared binding-safe
# generation primitive instead of replaying initial-lineage creation.
from .evidence_supplement_continuation import install as _install_evidence_supplement_continuation

_install_evidence_supplement_continuation()
del _install_evidence_supplement_continuation
