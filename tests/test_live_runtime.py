from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ues.identity import canonical_lane_id
from ues.live_runtime import BoundedJulesProbeClient, run_state_audit
from ues.providers.base import HttpResponse
from ues.state_store import OperationRead, OperationRecord, StateRead, WorkstreamRuntimeRecord


class FakeHttpTransport:
    def __init__(self, payload):
        self.payload = payload
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
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(self.payload).encode("utf-8"),
        )


class FakeAuditStore:
    def __init__(self):
        self.ready_lane = canonical_lane_id("UES", "INTERNAL:UES", "READY")
        self.blocked_lane = canonical_lane_id("UES", "INTERNAL:UES", "BLOCKED")
        self.operation_key = "ues-v2:test:" + "a" * 64

    def discover_lane_ids(self):
        return (self.ready_lane, self.blocked_lane)

    def discover_operation_keys(self):
        return (self.operation_key,)

    def read_operation(self, key):
        return OperationRead(
            "OK",
            1,
            OperationRecord(
                operation_key=key,
                lane_id=self.blocked_lane,
                workstream_id="BLOCKED",
                action="waiting-answer",
                request_digest="b" * 64,
                state="UNKNOWN",
                owner="runner",
                started_at="2026-08-24T00:00:00Z",
                updated_at="2026-08-24T00:00:00Z",
                reconciliation_required=True,
            ),
        )

    def read_workstream(self, lane_id):
        workstream = "READY" if lane_id == self.ready_lane else "BLOCKED"
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project="UES",
            route="INTERNAL:UES",
            workstream_id=workstream,
            activation_mode="SHADOW",
        )
        return StateRead("OK", 1, record, "SHADOW", False, None, False)


class LiveRuntimeTests(unittest.TestCase):
    def test_jules_probe_is_exactly_one_read_and_never_follows_pagination(self):
        transport = FakeHttpTransport(
            {
                "sessions": [{"name": "sessions/redacted-by-result"}],
                "nextPageToken": "must-not-be-followed",
            }
        )
        client = BoundedJulesProbeClient("runtime-only-secret", transport=transport)
        result = client.probe_authentication()
        self.assertTrue(result["authenticated_read_succeeded"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["mutation_performed"])
        self.assertEqual(result["page_size_requested"], 1)
        self.assertEqual(result["items_observed_on_page"], 1)
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertIsNone(request["body"])
        self.assertIn("pageSize=1", request["url"])
        self.assertNotIn("runtime-only-secret", json.dumps(result))
        self.assertNotIn("sessions/redacted-by-result", json.dumps(result))

    def test_runtime_watchdog_blocks_unknown_lane_without_freezing_ready_lane(self):
        with patch("ues.live_runtime.build_live_state_store", return_value=FakeAuditStore()):
            result = run_state_audit()
        self.assertEqual(result["cycle_status"], "CONTROL_CYCLE_FAILED")
        self.assertEqual(result["unresolved_operation_count"], 1)
        self.assertEqual(result["operation_state_counts"], {"UNKNOWN": 1})
        self.assertFalse(result["blocked_lane_freezes_independent_lanes"])
        self.assertEqual(len(result["blocked_lanes"]), 1)
        self.assertEqual(len(result["executable_lanes"]), 1)
        self.assertFalse(result["mutation_performed"])


if __name__ == "__main__":
    unittest.main()
