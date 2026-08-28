from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.identity import canonical_lane_id
from ues.state_backends.github_refs import BACKEND_SCHEMA, GitHubRefTransportError
from ues.state_backends.public_same_repo import (
    OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY,
    OwnerAuthorizedSameRepoGitDataTransport,
    OwnerAuthorizedSameRepoStateStore,
)
from ues.state_store import SCHEMA_VERSION, StateUnavailable


class DiscoveryTransport:
    repository = "hamad933/universal-execution-system"
    storage_policy = OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY
    storage_visibility = "PUBLIC"

    def __init__(self):
        self.refs = {}
        self.snapshots = {}

    def assert_private_repository(self):
        return None

    def list_refs(self, prefix):
        return {ref: sha for ref, sha in self.refs.items() if ref.startswith(prefix)}

    def read_snapshot(self, sha):
        return self.snapshots[sha]

    def get_ref(self, ref):
        return self.refs.get(ref)

    def create_snapshot_commit(self, **kwargs):  # pragma: no cover - discovery-only fake
        raise AssertionError("write not expected")

    def create_ref(self, ref, commit_sha):  # pragma: no cover - discovery-only fake
        raise AssertionError("write not expected")

    def update_ref(self, ref, commit_sha):  # pragma: no cover - discovery-only fake
        raise AssertionError("write not expected")


class PublicSameRepoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_constructor_requires_exact_same_repository_identity(self):
        with self.assertRaises(ValueError):
            OwnerAuthorizedSameRepoGitDataTransport(
                "hamad933/universal-execution-system",
                "secret",
                expected_repository="hamad933/other-repo",
            )

    def test_exact_public_repository_is_allowed_only_by_explicit_transport(self):
        transport = OwnerAuthorizedSameRepoGitDataTransport(
            "hamad933/universal-execution-system",
            "secret-value",
            expected_repository="hamad933/universal-execution-system",
        )
        transport._request_json = lambda method, path: {
            "full_name": "hamad933/universal-execution-system",
            "private": False,
        }
        transport.assert_private_repository()
        self.assertEqual(transport.storage_visibility, "PUBLIC")
        self.assertEqual(transport.storage_policy, OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY)
        self.assertNotIn("secret-value", repr(transport))
        self.assertIn("[REDACTED]", repr(transport))

    def test_remote_repository_identity_mismatch_fails_closed(self):
        transport = OwnerAuthorizedSameRepoGitDataTransport(
            "hamad933/universal-execution-system",
            "secret",
            expected_repository="hamad933/universal-execution-system",
        )
        transport._request_json = lambda method, path: {
            "full_name": "hamad933/different-repo",
            "private": False,
        }
        with self.assertRaises(GitHubRefTransportError):
            transport.assert_private_repository()

    def test_store_reports_explicit_same_repo_backend_name(self):
        transport = OwnerAuthorizedSameRepoGitDataTransport(
            "hamad933/universal-execution-system",
            "secret",
            expected_repository="hamad933/universal-execution-system",
        )
        transport._request_json = lambda method, path: {
            "full_name": "hamad933/universal-execution-system",
            "private": False,
        }
        store = OwnerAuthorizedSameRepoStateStore(transport, ref_prefix="ues-runtime/v2")
        self.assertEqual(
            store.capabilities.backend_name,
            "github-owner-authorized-same-repo-ref-cas-v1",
        )
        self.assertTrue(store.capabilities.atomic_compare_and_swap)
        self.assertTrue(store.capabilities.survives_runner_replacement)

    def test_discovery_recovers_embedded_lane_identity_and_rejects_ref_spoofing(self):
        transport = DiscoveryTransport()
        store = OwnerAuthorizedSameRepoStateStore(transport, ref_prefix="ues-runtime/v2")
        lane = canonical_lane_id("UES", "INTERNAL:UES", "W01")
        ref = store.lane_ref(lane)
        transport.refs[ref] = "commit-a"
        transport.snapshots["commit-a"] = {
            "backend_schema": BACKEND_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "lane",
            "identity": lane,
            "version": 1,
            "record": {"lane_id": lane},
        }
        self.assertEqual(store.discover_lane_ids(), (lane,))

        transport.refs = {ref: "commit-b"}
        transport.snapshots["commit-b"] = {
            "backend_schema": BACKEND_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "lane",
            "identity": canonical_lane_id("UES", "INTERNAL:UES", "OTHER"),
            "version": 1,
            "record": {"lane_id": lane},
        }
        with self.assertRaises(StateUnavailable):
            store.discover_lane_ids()


if __name__ == "__main__":
    unittest.main()
