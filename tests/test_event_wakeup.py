from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ues.event_wakeup import register_wakeup
from ues.state_store import DeterministicFileStateStore


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
        self.assertEqual(second["decision"], "DUPLICATE_EVENT_COALESCED")
        self.assertFalse(second["wakeup"])

    def test_events_are_lane_local_and_do_not_freeze_unrelated_project(self) -> None:
        event = {"type": "WRITER_COMPLETED", "event_id": "jules-1", "source": "jules"}
        cep = register_wakeup(self.store, project="CEP", route="PERSONAL:CEP", event=event)
        gs = register_wakeup(self.store, project="GS", route="GS", event=event)
        self.assertTrue(cep["wakeup"])
        self.assertTrue(gs["wakeup"])
        self.assertNotEqual(cep["lane_id"], gs["lane_id"])

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
