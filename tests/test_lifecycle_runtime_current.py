from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.lifecycle_runtime_current import (
    _handle_current_dispatches,
    _waiting_response,
    _workstream_pr_number,
    _workstream_pr_state_current,
)
from ues.state_store import DeterministicFileStateStore


class FakeGitHub:
    def __init__(self):
        self.dispatches = []

    def get_pull_request(self, owner, repo, number):
        return {
            "number": number,
            "state": "open",
            "merged": False,
            "head_ref": "work/cep-w05-parent-reconciliation-r01-8203175519000699108" if number == 34 else "remediation/gs-dependency-remediation-r1",
            "head_sha": "a" * 40 if number == 34 else "511ae72e49d258a14036548fedb7f6ca6f265352",
        }

    def get_required_ci_evidence(self, owner, repo, sha, specs):
        return {"verdict": "PASS", "sha": sha}

    def verify_exact_head(self, owner, repo, ref, expected_sha):
        return {"exact_head_match": True, "live_sha": expected_sha}

    def dispatch_workflow_bounded(self, owner, repo, **kwargs):
        self.dispatches.append((owner, repo, kwargs))
        return {
            "repository": f"{owner}/{repo}",
            "workflow": kwargs["workflow"],
            "ref": kwargs["ref"],
            "head_sha": kwargs["expected_sha"],
            "run_id": 9001,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "authoritative_readback": True,
        }


class CurrentLifecycleIntegrationTests(unittest.TestCase):
    def test_gs_final_assurance_pr_is_discovered_without_fake_writer(self) -> None:
        config = {
            "final_assurance": {
                "pr_number": 88,
                "replacement_prompt": "Independently assure exact SHA {current_sha}",
            }
        }
        self.assertEqual(_workstream_pr_number(config), 88)
        github = FakeGitHub()
        state = _workstream_pr_state_current(github, "hamad933/GS-2", config, [])
        self.assertEqual(state["pr"]["number"], 88)
        self.assertEqual(state["current_sha"], "511ae72e49d258a14036548fedb7f6ca6f265352")
        self.assertNotIn("writer", config)

    def test_conflicting_role_pr_identities_fail_closed(self) -> None:
        with self.assertRaises(Exception):
            _workstream_pr_number({"writer": {"pr_number": 1}, "reviewer": {"pr_number": 2}})

    def test_controller_resolvable_waiting_response_requires_exact_authority_entry(self) -> None:
        authority = {
            "waiting_responses": {
                "W04:WRITER": {
                    "controller_resolvable": True,
                    "scope_expansion": False,
                    "response": "Continue within the governed W04 scope.",
                }
            }
        }
        prompt = _waiting_response(authority, "W04", "WRITER")
        self.assertIn("Continue within the governed W04 scope.", prompt or "")
        self.assertIsNone(_waiting_response(authority, "W03", "WRITER"))

    def test_cep_w05_route_profile_dispatch_is_resolved_from_current_authority(self) -> None:
        adapter = json.loads(Path("adapters/cep.json").read_text(encoding="utf-8"))
        authority = {
            "authority_event_id": "CEP-W05-EVIDENCE-CURRENT",
            "lineages": {
                "W05": {
                    "writer": {"pr_number": 34}
                }
            },
            "workflow_dispatches": {
                "W05": {
                    "authorized": True,
                    "workflow_key": "release_browser",
                    "inputs": {"route_profiles": "W05"},
                    "purpose": "W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            github = FakeGitHub()
            results = _handle_current_dispatches(
                adapter=adapter,
                authority=authority,
                store=store,
                github=github,
            )
        self.assertEqual(results[0]["decision"], "WORKFLOW_DISPATCH_CONFIRMED")
        self.assertEqual(len(github.dispatches), 1)
        kwargs = github.dispatches[0][2]
        self.assertEqual(kwargs["inputs"], {"route_profiles": "W05"})
        self.assertEqual(kwargs["expected_sha"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
