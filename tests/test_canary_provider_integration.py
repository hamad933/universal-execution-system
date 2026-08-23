from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ues.canary_orchestrator import execute_waiting_answer_canary
from ues.idempotency import effect_operation_key, waiting_answer_effect_identity
from ues.identity import canonical_lane_id
from ues.providers.base import HttpResponse, NetworkError, RetryPolicy
from ues.providers.jules import JulesClient
from ues.routing import WAITING_SAME_SESSION_CONTINUATION
from ues.state_store import CanaryGrant, DeterministicFileStateStore, WorkstreamRuntimeRecord

UTC = timezone.utc
T0 = datetime(2026, 8, 24, 0, 40, tzinfo=UTC)
PROJECT = "SYNTH"
ROUTE = "INTERNAL:SYNTH"
WORKSTREAM = "W01"
LANE = canonical_lane_id(PROJECT, ROUTE, WORKSTREAM)
SESSION = "writer-session"
SOURCE = "sources/synth-source"
REPOSITORY = "owner/repo"
ACTIVITY = "waiting-activity"
AUTHORITY = "canary-authority-cross-layer"
OBSERVED_START = {
    "provider_state": "AWAITING_USER_FEEDBACK",
    "waiting_activity_id": ACTIVITY,
}


class FakeHttpTransport:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def response(status=200, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status=status, headers={}, body=body)


class CanaryProviderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(
            Path(self.tmp.name) / "runtime.json",
            clock=lambda: T0,
        )
        self.store.initialize()
        self.effect = waiting_answer_effect_identity(
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            session_id=SESSION,
            waiting_activity_id=ACTIVITY,
        )
        grant = CanaryGrant(
            authority_event_id=AUTHORITY,
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            effect_type=self.effect.action,
            target=dict(self.effect.target),
            issued_at=(T0 - timedelta(minutes=1)).isoformat(),
            expires_at=(T0 + timedelta(minutes=5)).isoformat(),
            maximum_effect_count=1,
            expected_start=dict(OBSERVED_START),
        )
        self.store.compare_and_swap_workstream(
            LANE,
            0,
            WorkstreamRuntimeRecord(
                lane_id=LANE,
                project=PROJECT,
                route=ROUTE,
                workstream_id=WORKSTREAM,
                activation_mode="CANARY",
                actor_bindings={
                    "WRITER": {
                        "provider": "jules",
                        "session_id": SESSION,
                        "proof_status": "PROVEN_EXPLICIT",
                        "source_repository": REPOSITORY,
                        "source_identity": SOURCE,
                        "evidence_id": "writer-proof",
                    },
                    "REVIEWER": {
                        "provider": "jules",
                        "session_id": "reviewer-session",
                        "proof_status": "PROVEN_EXPLICIT",
                        "source_repository": REPOSITORY,
                        "source_identity": SOURCE,
                    },
                },
                canary_grants=[grant],
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, client):
        return execute_waiting_answer_canary(
            self.store,
            client,
            lane_id=LANE,
            project=PROJECT,
            route=ROUTE,
            workstream_id=WORKSTREAM,
            session_id=SESSION,
            waiting_activity_id=ACTIVITY,
            expected_repository=REPOSITORY,
            expected_source=SOURCE,
            prompt="bounded answer",
            project_auto_safe_actions={WAITING_SAME_SESSION_CONTINUATION},
            project_policy_evidence_id="policy-proof",
            canary_authority_event_id=AUTHORITY,
            observed_start=OBSERVED_START,
            owner="runner",
            ttl_seconds=60,
            now=T0,
        )

    def test_http_success_then_post_read_network_failure_is_durable_unknown_without_resend(self):
        session = {
            "name": f"sessions/{SESSION}",
            "id": SESSION,
            "state": "AWAITING_USER_FEEDBACK",
            "sourceContext": {
                "source": SOURCE,
                "githubRepoContext": {"startingBranch": "work/example"},
            },
        }
        source = {
            "name": SOURCE,
            "id": "synth-source",
            "githubRepo": {"owner": "owner", "repo": "repo", "isPrivate": True},
        }
        old_activity = {
            "name": f"sessions/{SESSION}/activities/old",
            "originator": "agent",
            "agentMessaged": {"agentMessage": "question"},
        }
        transport = FakeHttpTransport(
            [
                response(payload=session),
                response(payload=source),
                response(payload={"activities": [old_activity]}),
                response(status=200),
                NetworkError("post-read timeout 1"),
                NetworkError("post-read timeout 2"),
                NetworkError("post-read timeout 3"),
            ]
        )
        sleeps = []
        client = JulesClient(
            "runtime-only-secret",
            transport=transport,
            read_retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay_seconds=0.01,
                max_delay_seconds=1,
            ),
            sleeper=sleeps.append,
        )

        first = self.execute(client)
        self.assertEqual(
            first["decision"],
            "WRITE_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
        )
        self.assertEqual(first["operation_state"], "UNKNOWN")
        self.assertEqual(first["stop_gate"], "AUTHORITATIVE_READBACK_REQUIRED")
        self.assertFalse(first["safe_to_blind_retry"])
        self.assertEqual(
            self.store.read_operation(effect_operation_key(self.effect)).record.state,
            "UNKNOWN",
        )
        self.assertEqual(len([r for r in transport.requests if r["method"] == "POST"]), 1)
        self.assertEqual(sleeps, [0.01, 0.02])
        self.assertNotIn("runtime-only-secret", json.dumps(first))

        second = self.execute(client)
        self.assertEqual(second["decision"], "RECONCILE_REQUIRED")
        self.assertEqual(len([r for r in transport.requests if r["method"] == "POST"]), 1)
        self.assertEqual(transport.steps, [])


if __name__ == "__main__":
    unittest.main()
