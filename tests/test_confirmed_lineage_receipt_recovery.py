from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.lineage_generation import recover_lineage_policy_from_state
from ues.lineage_registry import lineage_lane_id, session_fingerprint
from ues.state_store import DeterministicFileStateStore, WorkstreamRuntimeRecord


class ConfirmedLineageReceiptRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()
        self.project = "RP04"
        self.route = "RP04"
        self.workstream = "RP04-IPA-S07-001"
        self.role = "REVIEWER"
        self.lane_id = lineage_lane_id(self.project, self.route, self.workstream, self.role)
        self.fp = session_fingerprint("sessions/rp04-s07-reviewer")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_record(
        self,
        *,
        state: str = "CONFIRMED",
        creation_kind: str | None = "INITIAL_LOGICAL_LINEAGE",
        observed: bool = True,
        receipt_fp: str | None = None,
        evidence_fp: str | None = None,
    ) -> None:
        receipt: dict[str, object] = {
            "state": state,
            "generation": 1,
            "starting_branch": "review/rp04-s07",
            "transition_key": "transition-rp04-s07",
            "post_condition": {
                "observed": observed,
                "evidence": {
                    "session_fingerprint": receipt_fp or self.fp,
                    "repository": "hamad933/reference-product-04",
                    "starting_branch": "review/rp04-s07",
                    "generation": 1,
                    "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                    "initial_lineage_transition_key": "transition-rp04-s07",
                },
            },
        }
        if creation_kind is not None:
            receipt["creation_kind"] = creation_kind
        record = WorkstreamRuntimeRecord(
            lane_id=self.lane_id,
            project=self.project,
            route=self.route,
            workstream_id=f"LINEAGE::{self.workstream}::{self.role}",
            activation_mode="SHADOW",
            evidence_bindings={
                "generation": 0 if evidence_fp is None else 1,
                "session_fingerprint": evidence_fp,
                "provider_starting_branch": None,
                "generation_transition_key": None,
            },
            operation_receipt=receipt,
        )
        saved = self.store.compare_and_swap_workstream(self.lane_id, 0, record)
        self.assertEqual(saved.status, "OK")

    def _recover(self) -> dict[str, object]:
        return recover_lineage_policy_from_state(
            self.store,
            project=self.project,
            route=self.route,
            workstream=self.workstream,
            role=self.role,
            stable_policy={"known_session_fingerprints": []},
        )

    def test_confirmed_initial_creation_receipt_restores_exact_identity_and_diagnostics(self) -> None:
        self._write_record()
        recovered = self._recover()
        self.assertTrue(recovered["_state_generation_recovered"])
        self.assertEqual(recovered["_state_generation_recovery_source"], "CONFIRMED_CREATION_RECEIPT")
        self.assertEqual(recovered["_state_generation"], 1)
        self.assertEqual(recovered["_state_transition_key"], "transition-rp04-s07")
        self.assertEqual(recovered["provider_starting_branch"], "review/rp04-s07")
        self.assertEqual(recovered["source_repository"], "hamad933/reference-product-04")
        self.assertEqual(recovered["known_session_fingerprints"], [self.fp])

    def test_current_evidence_binding_takes_precedence_over_older_receipt(self) -> None:
        current_fp = session_fingerprint("sessions/current-authoritative")
        self._write_record(evidence_fp=current_fp)
        recovered = self._recover()
        self.assertEqual(recovered["_state_generation_recovery_source"], "EVIDENCE_BINDINGS")
        self.assertEqual(recovered["known_session_fingerprints"], [current_fp])

    def test_nonconfirmed_or_unobserved_receipt_never_recovers_identity(self) -> None:
        for state, observed in (("IN_FLIGHT", True), ("REJECTED", True), ("CONFIRMED", False)):
            with self.subTest(state=state, observed=observed):
                self.store = DeterministicFileStateStore(Path(self.temp.name) / f"{state}-{observed}.json")
                self.store.initialize()
                self._write_record(state=state, observed=observed)
                recovered = self._recover()
                self.assertFalse(recovered["_state_generation_recovered"])
                self.assertEqual(recovered["known_session_fingerprints"], [])

    def test_missing_creation_kind_or_malformed_fingerprint_never_recovers_identity(self) -> None:
        cases = (
            {"creation_kind": None, "receipt_fp": self.fp},
            {"creation_kind": "NEXT_PHYSICAL_GENERATION", "receipt_fp": self.fp},
            {"creation_kind": "INITIAL_LOGICAL_LINEAGE", "receipt_fp": "z" * 64},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                self.store = DeterministicFileStateStore(Path(self.temp.name) / f"invalid-{index}.json")
                self.store.initialize()
                self._write_record(**case)
                recovered = self._recover()
                self.assertFalse(recovered["_state_generation_recovered"])
                self.assertEqual(recovered["known_session_fingerprints"], [])


if __name__ == "__main__":
    unittest.main()
