import json
import unittest

from ues.providers.base import (
    AuthenticationError,
    AuthorizationError,
    HttpResponse,
    NetworkError,
    NotFoundError,
    ProtocolError,
    SessionContinuationUnavailable,
    RetryPolicy,
    WriteOutcomeUnknown,
)
from ues.providers.jules import JulesClient, normalize_session_state


class FakeTransport:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        if not self.steps:
            raise AssertionError(f"unexpected request {method} {url}")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def response(status=200, payload=None, headers=None):
    if payload is None:
        body = b""
    else:
        body = json.dumps(payload).encode()
    return HttpResponse(status=status, headers=headers or {}, body=body)


SESSION = {"name": "sessions/123", "id": "123", "state": "AWAITING_USER_FEEDBACK"}
PRE_ACTIVITY = {
    "name": "sessions/123/activities/old",
    "originator": "agent",
    "agentMessaged": {"agentMessage": "question"},
}
POST_ACTIVITY = {
    "name": "sessions/123/activities/new",
    "originator": "user",
    "userMessaged": {"userMessage": "continue"},
}


class JulesProviderTests(unittest.TestCase):
    def client(self, steps, sleeps=None):
        transport = FakeTransport(steps)
        sleeps = [] if sleeps is None else sleeps
        return (
            JulesClient(
                "super-secret-key",
                transport=transport,
                read_retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01, max_delay_seconds=1),
                sleeper=sleeps.append,
            ),
            transport,
            sleeps,
        )

    def test_sessions_pagination(self):
        client, transport, _ = self.client(
            [
                response(payload={"sessions": [{"id": "1", "state": "QUEUED"}], "nextPageToken": "nxt"}),
                response(payload={"sessions": [{"id": "2", "state": "COMPLETED"}]}),
            ]
        )
        result = client.list_sessions(page_size=1)
        self.assertEqual([item["id"] for item in result], ["1", "2"])
        self.assertIn("pageToken=nxt", transport.requests[1]["url"])
        self.assertEqual(result[0]["normalizedState"], "QUEUED")

    def test_activities_pagination(self):
        client, transport, _ = self.client(
            [
                response(payload={"activities": [{"name": "a1"}], "nextPageToken": "two"}),
                response(payload={"activities": [{"name": "a2"}]}),
            ]
        )
        result = client.list_activities("123", page_size=1)
        self.assertEqual([item["name"] for item in result], ["a1", "a2"])
        self.assertIn("pageToken=two", transport.requests[1]["url"])

    def test_current_jules_state_normalization(self):
        documented = [
            "QUEUED",
            "PLANNING",
            "AWAITING_PLAN_APPROVAL",
            "AWAITING_USER_FEEDBACK",
            "IN_PROGRESS",
            "PAUSED",
            "FAILED",
            "COMPLETED",
        ]
        for state in documented:
            self.assertEqual(normalize_session_state(state), state)

    def test_unknown_state_fails_closed(self):
        self.assertEqual(normalize_session_state(None), "UNKNOWN")
        self.assertEqual(normalize_session_state("STATE_UNSPECIFIED"), "UNKNOWN")
        self.assertEqual(normalize_session_state("NEW_FUTURE_STATE"), "UNKNOWN")
        client, _, _ = self.client([response(payload={"name": "sessions/123", "state": "FUTURE"})])
        result = client.get_session("123")
        self.assertEqual(result["normalizedState"], "UNKNOWN")
        self.assertFalse(result["stateAuthoritative"])

    def test_send_message_payload_contract_and_successful_readback(self):
        client, transport, _ = self.client(
            [
                response(payload=SESSION),
                response(payload={"activities": [PRE_ACTIVITY]}),
                response(status=200),
                response(payload={**SESSION, "state": "IN_PROGRESS"}),
                response(payload={"activities": [PRE_ACTIVITY, POST_ACTIVITY]}),
            ]
        )
        receipt = client.send_message("123", "continue")
        posts = [req for req in transport.requests if req["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["url"].endswith("/v1alpha/sessions/123:sendMessage"))
        self.assertEqual(json.loads(posts[0]["body"]), {"prompt": "continue"})
        self.assertEqual(receipt["outcome"], "DELIVERED")
        self.assertEqual(receipt["activity"], POST_ACTIVITY["name"])
        self.assertNotIn("super-secret-key", repr(client))
        self.assertNotIn("prompt", receipt)

    def test_ambiguous_write_outcome_recovery_confirms_delivery(self):
        client, transport, _ = self.client(
            [
                response(payload=SESSION),
                response(payload={"activities": [PRE_ACTIVITY]}),
                NetworkError("timeout"),
                response(payload={**SESSION, "state": "IN_PROGRESS"}),
                response(payload={"activities": [PRE_ACTIVITY, POST_ACTIVITY]}),
            ]
        )
        receipt = client.send_message("123", "continue")
        self.assertEqual(receipt["outcome"], "DELIVERED_AFTER_AMBIGUOUS_WRITE")
        self.assertEqual(len([req for req in transport.requests if req["method"] == "POST"]), 1)

    def test_ambiguous_write_without_delivery_never_blind_retries(self):
        client, transport, _ = self.client(
            [
                response(payload=SESSION),
                response(payload={"activities": [PRE_ACTIVITY]}),
                response(status=500),
                response(payload=SESSION),
                response(payload={"activities": [PRE_ACTIVITY]}),
            ]
        )
        with self.assertRaises(WriteOutcomeUnknown) as ctx:
            client.send_message("123", "continue")
        self.assertEqual(ctx.exception.recovery["verdict"], "WRITE_NOT_OBSERVED_AFTER_AUTHORITATIVE_READ")
        self.assertFalse(ctx.exception.recovery["safe_to_blind_retry"])
        self.assertEqual(len([req for req in transport.requests if req["method"] == "POST"]), 1)

    def test_definitive_write_http_errors(self):
        for status, error_type in [(401, AuthenticationError), (403, AuthorizationError), (404, NotFoundError)]:
            with self.subTest(status=status):
                client, transport, _ = self.client(
                    [response(payload=SESSION), response(payload={"activities": []}), response(status=status)]
                )
                with self.assertRaises(error_type):
                    client.send_message("123", "continue")
                self.assertEqual(len([req for req in transport.requests if req["method"] == "POST"]), 1)

    def test_write_429_respects_retry_after_but_does_not_retry_mutation(self):
        client, transport, sleeps = self.client(
            [
                response(payload=SESSION),
                response(payload={"activities": []}),
                response(status=429, headers={"Retry-After": "7"}),
                response(payload=SESSION),
                response(payload={"activities": []}),
            ]
        )
        with self.assertRaises(WriteOutcomeUnknown) as ctx:
            client.send_message("123", "continue")
        self.assertEqual(ctx.exception.retry_after, 7.0)
        self.assertEqual(sleeps, [])
        self.assertEqual(len([req for req in transport.requests if req["method"] == "POST"]), 1)

    def test_read_429_retries_with_retry_after(self):
        client, transport, sleeps = self.client(
            [response(status=429, headers={"Retry-After": "3"}), response(payload=SESSION)]
        )
        result = client.get_session("123")
        self.assertEqual(result["normalizedState"], "AWAITING_USER_FEEDBACK")
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(len(transport.requests), 2)

    def test_terminal_session_cannot_be_continued(self):
        client, transport, _ = self.client([response(payload={**SESSION, "state": "COMPLETED"})])
        with self.assertRaises(SessionContinuationUnavailable):
            client.send_message("123", "continue")
        self.assertEqual(len([req for req in transport.requests if req["method"] == "POST"]), 0)

    def test_protocol_error_on_malformed_json(self):
        client, _, _ = self.client([HttpResponse(status=200, headers={}, body=b"not-json")])
        with self.assertRaises(ProtocolError):
            client.get_session("123")

    def test_network_timeout_read_is_bounded(self):
        client, transport, sleeps = self.client(
            [NetworkError("timeout"), NetworkError("timeout"), NetworkError("timeout")]
        )
        with self.assertRaises(NetworkError):
            client.get_session("123")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
