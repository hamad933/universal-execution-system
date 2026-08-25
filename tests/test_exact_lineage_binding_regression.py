from __future__ import annotations

import unittest

from ues.lineage_registry import match_lineage_session, session_fingerprint


class ExactLineageBindingRegressionTests(unittest.TestCase):
    def test_multiple_exact_active_fingerprint_matches_are_ambiguous(self) -> None:
        fp1 = session_fingerprint("sessions/1")
        fp2 = session_fingerprint("sessions/2")
        sessions = [
            {
                "name": "sessions/1",
                "normalizedState": "IN_PROGRESS",
                "_source_repository": "owner/repo",
                "_session_fingerprint": fp1,
                "sourceStartingBranch": "provider/shared",
                "updateTime": "2026-08-24T00:00:00Z",
            },
            {
                "name": "sessions/2",
                "normalizedState": "IN_PROGRESS",
                "_source_repository": "owner/repo",
                "_session_fingerprint": fp2,
                "sourceStartingBranch": "provider/shared",
                "updateTime": "2026-08-24T00:00:00Z",
            },
        ]
        result = match_lineage_session(
            sessions,
            {
                "provider_starting_branch": "provider/shared",
                "known_session_fingerprints": [fp1, fp2],
            },
            repository="owner/repo",
        )
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["reason"], "MULTIPLE_EXACT_LINEAGE_SESSION_MATCHES")


if __name__ == "__main__":
    unittest.main()
