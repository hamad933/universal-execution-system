import json
import unittest

from ues.providers.base import (
    AuthenticationError,
    AuthorizationError,
    HttpResponse,
    NetworkError,
    NotFoundError,
    ProtocolError,
    RetryPolicy,
    SessionContinuationUnavailable,
    WriteOutcomeUnknown,
)
from ues.providers.jules import DEFAULT_MUTATION_STATES, JulesClient, normalize_session_state


class FakeTransport:
    def __init__(self, steps): self.steps=list(steps); self.requests=[]
    def request(self,method,url,*,headers,body,timeout):
        self.requests.append({"method":method,"url":url,"headers":dict(headers),"body":body,"timeout":timeout})
        if not self.steps: raise AssertionError(f"unexpected request {method} {url}")
        step=self.steps.pop(0)
        if isinstance(step,Exception): raise step
        return step

def response(status=200,payload=None,headers=None):
    body=b"" if payload is None else json.dumps(payload).encode()
    return HttpResponse(status=status,headers=headers or {},body=body)

SOURCE_CONTEXT={"source":"sources/src-1","githubRepoContext":{"startingBranch":"feature"}}
SESSION={"name":"sessions/123","id":"123","state":"AWAITING_USER_FEEDBACK","sourceContext":SOURCE_CONTEXT}
SOURCE={"name":"sources/src-1","id":"src-1","githubRepo":{"owner":"o","repo":"r","isPrivate":True}}
PRE_ACTIVITY={"name":"sessions/123/activities/old","originator":"agent","agentMessaged":{"agentMessage":"question"}}
POST_ACTIVITY={"name":"sessions/123/activities/new","originator":"user","userMessaged":{"userMessage":"continue"}}

class JulesProviderTests(unittest.TestCase):
    def client(self,steps,sleeps=None,allowed_mutation_states=None):
        transport=FakeTransport(steps); sleeps=[] if sleeps is None else sleeps
        kwargs={}
        if allowed_mutation_states is not None: kwargs["allowed_mutation_states"]=allowed_mutation_states
        return JulesClient("super-secret-key",transport=transport,read_retry_policy=RetryPolicy(max_attempts=3,base_delay_seconds=.01,max_delay_seconds=1),sleeper=sleeps.append,**kwargs),transport,sleeps

    def success_steps(self,state="AWAITING_USER_FEEDBACK"):
        pre={**SESSION,"state":state}; post={**SESSION,"state":"IN_PROGRESS"}
        return [response(payload=pre),response(payload=SOURCE),response(payload={"activities":[PRE_ACTIVITY]}),response(status=200),response(payload=post),response(payload={"activities":[PRE_ACTIVITY,POST_ACTIVITY]})]

    def test_sessions_pagination(self):
        client,transport,_=self.client([response(payload={"sessions":[{"id":"1","state":"QUEUED"}],"nextPageToken":"nxt"}),response(payload={"sessions":[{"id":"2","state":"COMPLETED"}]})])
        result=client.list_sessions(page_size=1); self.assertEqual([i["id"] for i in result],["1","2"]); self.assertIn("pageToken=nxt",transport.requests[1]["url"])

    def test_sources_pagination(self):
        client,transport,_=self.client([response(payload={"sources":[SOURCE],"nextPageToken":"nxt"}),response(payload={"sources":[]})])
        result=client.list_sources(page_size=1); self.assertEqual(result[0]["repository"],"o/r"); self.assertIn("pageToken=nxt",transport.requests[1]["url"])

    def test_activities_pagination(self):
        client,transport,_=self.client([response(payload={"activities":[{"name":"a1"}],"nextPageToken":"two"}),response(payload={"activities":[{"name":"a2"}]})])
        result=client.list_activities("123",page_size=1); self.assertEqual([i["name"] for i in result],["a1","a2"]); self.assertIn("pageToken=two",transport.requests[1]["url"])

    def test_current_state_normalization(self):
        for state in ["QUEUED","PLANNING","AWAITING_PLAN_APPROVAL","AWAITING_USER_FEEDBACK","IN_PROGRESS","PAUSED","FAILED","COMPLETED"]: self.assertEqual(normalize_session_state(state),state)
        self.assertEqual(normalize_session_state("STATE_UNSPECIFIED"),"UNKNOWN")

    def test_default_mutation_states_are_strict(self): self.assertEqual(DEFAULT_MUTATION_STATES,{"AWAITING_USER_FEEDBACK","IN_PROGRESS"})

    def test_send_message_allowed_in_awaiting_user_feedback(self):
        client,transport,_=self.client(self.success_steps())
        receipt=client.send_message("123","continue",expected_repository="o/r")
        self.assertEqual(receipt["outcome"],"DELIVERED"); self.assertEqual(receipt["repository"],"o/r"); self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_send_message_allowed_in_progress(self):
        client,transport,_=self.client(self.success_steps("IN_PROGRESS"))
        receipt=client.send_message("123","continue",expected_repository=("o","r"))
        self.assertEqual(receipt["outcome"],"DELIVERED"); self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_non_mutation_states_never_post(self):
        cases=[("QUEUED",ProtocolError),("PLANNING",ProtocolError),("AWAITING_PLAN_APPROVAL",ProtocolError),("PAUSED",ProtocolError),("FAILED",SessionContinuationUnavailable),("COMPLETED",SessionContinuationUnavailable),("FUTURE",ProtocolError)]
        for state,error in cases:
            with self.subTest(state=state):
                client,transport,_=self.client([response(payload={**SESSION,"state":state})])
                with self.assertRaises(error): client.send_message("123","continue",expected_repository="o/r")
                self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),0)

    def test_project_policy_can_only_narrow_mutation_states(self):
        client,transport,_=self.client([response(payload={**SESSION,"state":"IN_PROGRESS"})],allowed_mutation_states={"AWAITING_USER_FEEDBACK"})
        with self.assertRaises(ProtocolError): client.send_message("123","continue",expected_repository="o/r")
        self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),0)
        with self.assertRaises(ValueError): self.client([],allowed_mutation_states={"PLANNING"})

    def test_explicit_source_binding_evidence_is_proven(self):
        client,_,_=self.client([response(payload=SESSION),response(payload=SOURCE)])
        result=client.get_session_source_binding("123",expected_repository="o/r")
        self.assertTrue(result["proven"]); self.assertEqual(result["verification"],"PROVEN_EXPLICIT_SOURCE"); self.assertEqual(result["source"],"sources/src-1"); self.assertTrue(result["matches_expected_repository"]); self.assertEqual(result["starting_branch"],"feature")

    def test_unique_heuristic_source_match_remains_unverified(self):
        session={k:v for k,v in SESSION.items() if k!="sourceContext"}
        client,transport,_=self.client([response(payload=session)])
        result=client.get_session_source_binding("123",expected_repository="o/r",heuristic_candidates=["o/r"])
        self.assertFalse(result["proven"]); self.assertEqual(result["verification"],"PROPOSED_UNVERIFIED"); self.assertFalse(result["matches_expected_repository"]); self.assertEqual(len(transport.requests),1)

    def test_mutation_requires_expected_repository_and_explicit_binding(self):
        client,transport,_=self.client([response(payload=SESSION)])
        with self.assertRaises(ProtocolError): client.send_message("123","continue")
        self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),0)
        client,transport,_=self.client([response(payload=SESSION),response(payload=SOURCE)])
        with self.assertRaises(ProtocolError): client.send_message("123","continue",expected_repository="other/repo")
        self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),0)

    def test_payload_contract_and_secret_redaction(self):
        client,transport,_=self.client(self.success_steps())
        receipt=client.send_message("123","continue",expected_repository="o/r",expected_source="sources/src-1")
        post=[r for r in transport.requests if r["method"]=="POST"][0]
        self.assertEqual(json.loads(post["body"]),{"prompt":"continue"}); self.assertNotIn("super-secret-key",repr(client)); self.assertNotIn("prompt",receipt)

    def test_ambiguous_write_recovery_confirms_without_retry(self):
        steps=[response(payload=SESSION),response(payload=SOURCE),response(payload={"activities":[PRE_ACTIVITY]}),NetworkError("timeout"),response(payload={**SESSION,"state":"IN_PROGRESS"}),response(payload={"activities":[PRE_ACTIVITY,POST_ACTIVITY]})]
        client,transport,_=self.client(steps); receipt=client.send_message("123","continue",expected_repository="o/r")
        self.assertEqual(receipt["outcome"],"DELIVERED_AFTER_AMBIGUOUS_WRITE"); self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_ambiguous_write_unobserved_never_blind_retries(self):
        steps=[response(payload=SESSION),response(payload=SOURCE),response(payload={"activities":[PRE_ACTIVITY]}),response(status=500),response(payload=SESSION),response(payload={"activities":[PRE_ACTIVITY]})]
        client,transport,_=self.client(steps)
        with self.assertRaises(WriteOutcomeUnknown) as ctx: client.send_message("123","continue",expected_repository="o/r")
        self.assertFalse(ctx.exception.recovery["safe_to_blind_retry"]); self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_definitive_http_errors(self):
        for status,error in [(401,AuthenticationError),(403,AuthorizationError),(404,NotFoundError)]:
            with self.subTest(status=status):
                client,transport,_=self.client([response(payload=SESSION),response(payload=SOURCE),response(payload={"activities":[]}),response(status=status)])
                with self.assertRaises(error): client.send_message("123","continue",expected_repository="o/r")
                self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_write_429_reconciles_without_retry(self):
        client,transport,sleeps=self.client([response(payload=SESSION),response(payload=SOURCE),response(payload={"activities":[]}),response(status=429,headers={"Retry-After":"7"}),response(payload=SESSION),response(payload={"activities":[]})])
        with self.assertRaises(WriteOutcomeUnknown) as ctx: client.send_message("123","continue",expected_repository="o/r")
        self.assertEqual(ctx.exception.retry_after,7.0); self.assertEqual(sleeps,[]); self.assertEqual(len([r for r in transport.requests if r["method"]=="POST"]),1)

    def test_read_429_retries_with_retry_after(self):
        client,transport,sleeps=self.client([response(status=429,headers={"Retry-After":"3"}),response(payload=SESSION)])
        result=client.get_session("123"); self.assertEqual(result["normalizedState"],"AWAITING_USER_FEEDBACK"); self.assertEqual(sleeps,[3.0]); self.assertEqual(len(transport.requests),2)

if __name__=="__main__": unittest.main()
