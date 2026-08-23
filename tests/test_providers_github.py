import json
import unittest

from ues.providers.base import HttpResponse, RetryPolicy
from ues.providers.github import GitHubClient

class FakeTransport:
    def __init__(self,steps): self.steps=list(steps); self.requests=[]
    def request(self,method,url,*,headers,body,timeout):
        self.requests.append({"method":method,"url":url,"headers":dict(headers),"body":body})
        if not self.steps: raise AssertionError(f"unexpected request {method} {url}")
        return self.steps.pop(0)
def response(payload,status=200): return HttpResponse(status=status,headers={},body=json.dumps(payload).encode())
SHA="a"*40; OTHER="b"*40

class GitHubProviderTests(unittest.TestCase):
    def client(self,steps):
        t=FakeTransport(steps); return GitHubClient("gh-secret-token",transport=t,read_retry_policy=RetryPolicy(max_attempts=1),sleeper=lambda _:None),t

    def test_unscoped_ci_observation_never_authorizes_pass(self):
        client,_=self.client([response([{"id":1,"sha":SHA,"state":"success","context":"unrelated"}]),response({"check_runs":[{"id":2,"name":"other","head_sha":SHA,"status":"completed","conclusion":"success"}]})])
        result=client.get_ci_evidence("o","r",SHA)
        self.assertEqual(result["aggregate"],"PASS"); self.assertEqual(result["verdict"],"NOT_A_PASS"); self.assertFalse(result["pass_authorized"]); self.assertFalse(result["required_ci_evaluated"])

    def test_unrelated_green_check_cannot_satisfy_required_ci(self):
        client,_=self.client([response([]),response({"check_runs":[{"id":2,"name":"unrelated","head_sha":SHA,"status":"completed","conclusion":"success"}]})])
        result=client.get_required_ci_evidence("o","r",SHA,[{"kind":"check","name":"required-unit"}])
        self.assertEqual(result["verdict"],"NOT_A_PASS"); self.assertEqual(result["reason"],"REQUIRED_CI_MISSING"); self.assertFalse(result["pass_authorized"])

    def test_missing_required_ci_is_not_a_pass(self):
        client,_=self.client([response([]),response({"check_runs":[]})])
        result=client.get_required_ci_evidence("o","r",SHA,[{"kind":"status","context":"required/context"}])
        self.assertEqual(result["reason"],"REQUIRED_CI_MISSING"); self.assertFalse(result["evidence_complete"]); self.assertFalse(result["pass_authorized"])

    def test_required_ci_exact_sha_success_is_eligible(self):
        client,_=self.client([response([{"id":1,"sha":SHA,"state":"success","context":"required/context"}]),response({"check_runs":[{"id":2,"name":"required-unit","head_sha":SHA,"status":"completed","conclusion":"success"}]})])
        result=client.get_required_ci_evidence("o","r",SHA,[{"kind":"status","context":"required/context"},{"kind":"check","name":"required-unit"}])
        self.assertEqual(result["verdict"],"PASS"); self.assertEqual(result["reason"],"ALL_REQUIRED_CI_SATISFIED"); self.assertTrue(result["pass_authorized"]); self.assertTrue(result["exact_sha_match"])

    def test_required_ci_stale_exact_sha_fails_closed(self):
        client,_=self.client([response([{"id":1,"sha":OTHER,"state":"success","context":"required/context"}]),response({"check_runs":[]})])
        result=client.get_required_ci_evidence("o","r",SHA,[{"kind":"status","context":"required/context"}])
        self.assertEqual(result["reason"],"REQUIRED_CI_STALE_OR_MISMATCH"); self.assertFalse(result["pass_authorized"])

    def test_required_workflow_and_job_contract(self):
        run={"id":9,"workflow_id":55,"name":"Core CI","path":".github/workflows/core.yml","head_sha":SHA,"run_attempt":2,"status":"completed","conclusion":"success"}
        job={"id":10,"run_id":9,"head_sha":SHA,"name":"tests","status":"completed","conclusion":"success"}
        client,transport=self.client([response({"workflow_runs":[run]}),response({"jobs":[job]})])
        result=client.get_required_ci_evidence("o","r",SHA,[{"kind":"workflow","workflow":".github/workflows/core.yml"},{"kind":"job","workflow":55,"job":"tests"}])
        self.assertTrue(result["pass_authorized"]); self.assertTrue(any("/attempts/2/jobs" in r["url"] for r in transport.requests))

    def test_attempt_1_artifact_cannot_satisfy_attempt_2(self):
        run={"id":9,"head_sha":SHA,"run_attempt":2}
        job={"id":10,"run_id":9,"head_sha":SHA}
        artifact={"id":11,"name":"evidence","digest":"sha256:abc","run_attempt":1,"producer_job_id":10,"workflow_run":{"id":9,"head_sha":SHA}}
        client,_=self.client([response(run),response({"jobs":[job]}),response({"artifacts":[artifact]})])
        result=client.get_workflow_binding("o","r",9,expected_sha=SHA,expected_run_attempt=2,required_artifacts=[{"name":"evidence"}])
        self.assertFalse(result["binding_valid"]); self.assertEqual(result["artifact_lineage_status"],"MISMATCH"); self.assertEqual(result["artifact_mismatches"],[11])

    def test_artifact_lineage_unprovable_fails_closed(self):
        run={"id":9,"head_sha":SHA,"run_attempt":2}; job={"id":10,"run_id":9,"head_sha":SHA}
        artifact={"id":11,"name":"evidence","digest":"sha256:abc","workflow_run":{"id":9,"head_sha":SHA}}
        client,_=self.client([response(run),response({"jobs":[job]}),response({"artifacts":[artifact]})])
        result=client.get_workflow_binding("o","r",9,expected_sha=SHA,expected_run_attempt=2,required_artifacts=[{"name":"evidence"}])
        self.assertFalse(result["binding_valid"]); self.assertFalse(result["evidence_complete"]); self.assertEqual(result["artifact_lineage_status"],"UNPROVEN"); self.assertEqual(result["artifact_unproven"],[11])

    def test_explicit_attempt_artifact_lineage_can_be_proven(self):
        run={"id":9,"head_sha":SHA,"run_attempt":2}; job={"id":10,"run_id":9,"head_sha":SHA}
        artifact={"id":11,"name":"evidence","digest":"sha256:abc","run_attempt":2,"producer_job_id":10,"workflow_run":{"id":9,"head_sha":SHA}}
        client,_=self.client([response(run),response({"jobs":[job]}),response({"artifacts":[artifact]})])
        result=client.get_workflow_binding("o","r",9,expected_sha=SHA,expected_run_attempt=2,required_artifacts=[{"artifact_id":11,"digest":"sha256:abc","producer_job_id":10}])
        self.assertTrue(result["binding_valid"]); self.assertTrue(result["evidence_complete"]); self.assertEqual(result["artifacts"][0]["lineage_state"],"PROVEN")

    def test_missing_required_artifact_is_incomplete(self):
        run={"id":9,"head_sha":SHA,"run_attempt":2}; job={"id":10,"run_id":9,"head_sha":SHA}
        client,_=self.client([response(run),response({"jobs":[job]}),response({"artifacts":[]})])
        result=client.get_workflow_binding("o","r",9,expected_sha=SHA,expected_run_attempt=2,required_artifacts=[{"name":"browser-evidence"}])
        self.assertFalse(result["binding_valid"]); self.assertEqual(result["required_artifact_missing"],[{"name":"browser-evidence"}])

    def test_run_attempt_mismatch_fails_binding(self):
        client,_=self.client([response({"id":9,"head_sha":SHA,"run_attempt":3}),response({"jobs":[]}),response({"artifacts":[]})])
        result=client.get_workflow_binding("o","r",9,expected_sha=SHA,expected_run_attempt=2,required_artifacts=[{"name":"evidence"}])
        self.assertFalse(result["attempt_match"]); self.assertFalse(result["binding_valid"])

    def test_exact_head_mismatch(self):
        client,_=self.client([response({"ref":"refs/heads/feature","object":{"type":"commit","sha":OTHER}})])
        result=client.verify_exact_head("o","r","feature",SHA); self.assertFalse(result["exact_head_match"]); self.assertFalse(result["pass_authorized"])

    def test_pr_binding_includes_exact_head_and_base(self):
        client,_=self.client([response({"number":7,"state":"open","draft":True,"merged":False,"head":{"ref":"feature","sha":SHA},"base":{"ref":"main","sha":OTHER},"merge_commit_sha":None})])
        result=client.get_pull_request("o","r",7); self.assertEqual(result["head_sha"],SHA); self.assertEqual(result["base_sha"],OTHER); self.assertTrue(result["draft"])

    def test_reviewed_sha_missing_is_partial_evidence(self):
        client,_=self.client([response([{"id":1,"state":"APPROVED","commit_id":None}])])
        result=client.list_reviews("o","r",7,expected_sha=SHA); self.assertFalse(result["evidence_complete"]); self.assertFalse(result["all_reviews_exact_sha"])

    def test_secret_is_not_in_repr(self):
        client,_=self.client([]); self.assertNotIn("gh-secret-token",repr(client))

if __name__=="__main__": unittest.main()
