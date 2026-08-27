from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ues.event_wakeup import register_wakeup
from ues.state_store import (
    DeterministicFileStateStore,
    StateUnavailable,
    StateVersionConflict,
)


class ConflictingWakeupStore:
    def __init__(self, *, final_unavailable: bool = False):
        self.read_calls = 0
        self.cas_calls = 0
        self.final_unavailable = final_unavailable

    def read_workstream(self, lane_id: str):
        self.read_calls += 1
        if self.final_unavailable and self.read_calls >= 4:
            return SimpleNamespace(
                status="UNAVAILABLE",
                record=None,
                version=0,
                reason="authoritative event-lane read unavailable",
            )
        return SimpleNamespace(
            status="MISSING",
            record=None,
            version=0,
            reason=None,
        )

    def compare_and_swap_workstream(self, lane_id: str, expected_version: int, record):
        self.cas_calls += 1
        raise StateVersionConflict("simulated event-ingress contention")


class EventWakeupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeterministicFileStateStore(Path(self.temp.name) / "state.json")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_event_wakes_immediately_and_duplicate_is_coalesced(self) -> None:
        event = {
            "type": "CI_COMPLETED",
            "event_id": "run-123",
            "source": "github",
            "repository": "owner/repo",
            "workstream": "W05",
            "sha": "a" * 40,
        }
        first = register_wakeup(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            event=event,
        )
        second = register_wakeup(
            self.store,
            project="CEP",
            route="PERSONAL:CEP",
            event=event,
        )
        self.assertTrue(first["wakeup"])
        self.assertFalse(first["event_grants_mutation_authority"])
        self.assertTrue(first["coalescing_durable"])
        self.assertEqual(second["decision"], "DUPLICATE_EVENT_COALESCED")
        self.assertFalse(second["wakeup"])
        self.assertTrue(second["coalescing_durable"])

    def test_duplicate_external_reconciliation_continues_to_guarded_lifecycle(self) -> None:
        event = {
            "type": "EXTERNAL_RECONCILIATION_REQUEST",
            "event_id": "rp02-review-reconcile-001",
            "source": "github",
            "repository": "owner/repo",
            "workstream": "RP02-IPA-S01-001",
        }
        first = register_wakeup(
            self.store,
            project="RP02",
            route="RP02",
            event=event,
        )
        second = register_wakeup(
            self.store,
            project="RP02",
            route="RP02",
            event=event,
        )
        self.assertTrue(first["wakeup"])
        self.assertEqual(
            second["decision"],
            "DUPLICATE_EXTERNAL_RECONCILIATION_CONTINUE_GUARDED",
        )
        self.assertTrue(second["wakeup"])
        self.assertTrue(second["coalescing_durable"])
        self.assertFalse(second["event_grants_mutation_authority"])
        self.assertTrue(second["downstream_authority_reconstruction_required"])
        self.assertTrue(second["downstream_idempotency_and_unknown_checks_required"])
        self.assertFalse(second["safe_to_blind_retry"])

    def test_events_are_lane_local_and_do_not_freeze_unrelated_project(self) -> None:
        event = {"type": "WRITER_COMPLETED", "event_id": "jules-1", "source": "jules"}
        cep = register_wakeup(self.store, project="CEP", route="PERSONAL:CEP", event=event)
        gs = register_wakeup(self.store, project="GS", route="GS", event=event)
        self.assertTrue(cep["wakeup"])
        self.assertTrue(gs["wakeup"])
        self.assertNotEqual(cep["lane_id"], gs["lane_id"])

    def test_cas_contention_does_not_turn_non_authoritative_coalescing_into_global_stop(self) -> None:
        store = ConflictingWakeupStore()
        result = register_wakeup(
            store,
            project="RP02",
            route="RP02",
            event={
                "type": "EXTERNAL_RECONCILIATION_REQUEST",
                "event_id": "rp02-handoff-recovery-001",
                "source": "github",
                "repository": "owner/repo",
                "workstream": "RP02-IPA-S01-001",
                "sha": "b" * 40,
            },
        )
        self.assertEqual(store.cas_calls, 3)
        self.assertEqual(store.read_calls, 4)
        self.assertEqual(result["decision"], "EVENT_WAKEUP_COALESCING_NOT_DURABLE_CONTINUE_GUARDED")
        self.assertTrue(result["wakeup"])
        self.assertFalse(result["coalescing_durable"])
        self.assertFalse(result["event_grants_mutation_authority"])
        self.assertTrue(result["downstream_authority_reconstruction_required"])
        self.assertTrue(result["downstream_idempotency_and_unknown_checks_required"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_cas_contention_still_fails_closed_when_final_authoritative_read_is_unavailable(self) -> None:
        store = ConflictingWakeupStore(final_unavailable=True)
        with self.assertRaises(StateUnavailable):
            register_wakeup(
                store,
                project="RP02",
                route="RP02",
                event={
                    "type": "EXTERNAL_RECONCILIATION_REQUEST",
                    "event_id": "rp02-handoff-recovery-002",
                    "source": "github",
                },
            )
        self.assertEqual(store.cas_calls, 3)
        self.assertEqual(store.read_calls, 4)

    def test_unknown_event_type_cannot_expand_authority(self) -> None:
        with self.assertRaises(ValueError):
            register_wakeup(
                self.store,
                project="CEP",
                route="PERSONAL:CEP",
                event={"type": "ARBITRARY_MUTATION", "event_id": "x"},
            )


if __name__ == "__main__":
    unittest.main()
