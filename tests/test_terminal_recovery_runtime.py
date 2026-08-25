from __future__ import annotations

import unittest

from tests.test_terminal_recovery import FakeClient, FakeStore, _Read, _handoff, _record, _session
from ues.lineage_registry import session_fingerprint
from ues.state_store import StateUnavailable
from ues.terminal_recovery_runtime import run_read_only_backfill


class OutageAfterProviderReadStore(FakeStore):
    def __init__(self, records=None):
        super().__init__(records)
        self.outage = False

    def discover_lane_ids(self):
        if self.outage:
            raise StateUnavailable("StateStore unavailable after provider read")
        return super().discover_lane_ids()

    def read_workstream(self, lane_id):
        if self.outage:
            return _Read("UNAVAILABLE", reason="StateStore unavailable after provider read")
        return super().read_workstream(lane_id)


class OutageAfterActivityClient(FakeClient):
    def __init__(self, store, sessions, activities):
        super().__init__(sessions, activities)
        self.store = store

    def list_activities(self, session, *, page_size=100):
        value = super().list_activities(session, page_size=page_size)
        self.store.outage = True
        return value


class TerminalRecoveryRuntimeTests(unittest.TestCase):
    def test_provider_read_survives_statestore_outage_before_materialization_and_reports_partial_persistence(self):
        name = "sessions/rp02-post-read-outage"
        fp = session_fingerprint(name)
        lane, record = _record(fp=fp)
        store = OutageAfterProviderReadStore({lane: record})
        client = OutageAfterActivityClient(store, [_session(name)], {name: _handoff()})

        result = run_read_only_backfill(["RP02"], store=store, client=client)

        self.assertEqual(result["result"], "TERMINAL_BACKFILL_PARTIAL_STATESTORE_RECOVERY_REQUIRED")
        self.assertTrue(result["provider_read_complete"])
        self.assertEqual(result["provider_activity_content_reads"], 1)
        self.assertFalse(result["state_persistence_complete"])
        self.assertEqual(result["outcomes"][0]["result_state"], "PARENT_CONSUMABLE")
        self.assertEqual(
            result["outcomes"][0]["persistence_state"],
            "TERMINAL_RESULT_STATESTORE_UNAVAILABLE_AFTER_PROVIDER_READ",
        )
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertEqual(client.provider_mutations, 0)


if __name__ == "__main__":
    unittest.main()
