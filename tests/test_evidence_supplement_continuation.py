from __future__ import annotations

import json
import unittest

import ues.evidence_supplement_runtime as runtime
from ues.evidence_supplement_continuation import _continuation_prompt, _workstream_contract


CANDIDATE = "06d7e80af27232f416940d04dffe4a325b01e14d"


def lane() -> dict:
    return {
        "workstream": "RP03-IPA-S02-EVIDENCE-SUPPLEMENT",
        "role": "ASSURANCE",
        "target_ref": "main",
        "candidate_sha": CANDIDATE,
        "transport_repository_fingerprint": "sha256:" + "1" * 64,
        "transport_starting_branch": "ues-transport/rp03-evidence-supplement-20260827",
        "transport_head_sha": "2" * 40,
        "transport_attested_at": "2026-08-29T14:00:00Z",
        "evidence_root": "rp03-evidence-supplement/RP03-S02",
        "governed_packet_sha256": "3" * 64,
        "decoded_evidence_sha256": "4" * 64,
        "task_spec": {
            "objective": "Inspect only the previously missing governed evidence.",
            "exact_baseline": "main@" + CANDIDATE,
            "write_scope": [],
            "prohibited_scope": ["mutation"],
            "validation": ["verify hashes"],
            "evidence": ["governed packet"],
            "handoff": "return structured supplement result",
            "stop_gate": "RESULT_RETURNED",
        },
    }


class EvidenceSupplementContinuationTests(unittest.TestCase):
    def test_package_installs_same_lineage_continuation_once(self) -> None:
        self.assertTrue(getattr(runtime, "_same_lineage_continuation_installed", False))

    def test_continuation_contract_binds_exact_assurance_lineage(self) -> None:
        value = _workstream_contract(lane())
        self.assertEqual(value["role"], "ASSURANCE")
        self.assertEqual(value["logical_lineage"], "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")
        self.assertEqual(value["exact_baseline"], "main@" + CANDIDATE)
        self.assertEqual(value["write_scope"], [])
        self.assertTrue(value["validation"])
        self.assertTrue(value["evidence"])

    def test_continuation_prompt_contains_exactly_one_machine_contract(self) -> None:
        text = _continuation_prompt(runtime, lane())
        prefix = "PARENT_CONTROLLER_WORKSTREAM_CONTRACT_V1="
        self.assertEqual(text.count(prefix), 1)
        raw = [line for line in text.splitlines() if line.startswith(prefix)][0][len(prefix):]
        value = json.loads(raw)
        self.assertEqual(value["role"], "ASSURANCE")
        self.assertEqual(value["logical_lineage"], "RP03-IPA-S02-EVIDENCE-SUPPLEMENT")
        self.assertEqual(value["exact_baseline"], "main@" + CANDIDATE)
        self.assertEqual(value["write_scope"], [])


if __name__ == "__main__":
    unittest.main()
