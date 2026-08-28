from __future__ import annotations

import unittest
from pathlib import Path

from ues.jules_source_probe import parse_candidate_hashes, probe_sources, repository_fingerprint


class FakeClient:
    def __init__(self, sources):
        self.sources = list(sources)
        self.calls = []

    def list_sources(self, *, page_size=100):
        self.calls.append(("list_sources", page_size))
        return list(self.sources)


class JulesSourceProbeTests(unittest.TestCase):
    def test_probe_matches_only_candidate_hashes_without_persisting_names(self) -> None:
        wanted = repository_fingerprint("Example/Private-Evidence")
        other = repository_fingerprint("Example/Other")
        client = FakeClient(
            [
                {"repository": "Example/Private-Evidence", "name": "sources/private-secret-id"},
                {"repository": "Example/Other", "name": "sources/other-secret-id"},
                {"repository": None, "name": "sources/unbound"},
            ]
        )

        result = probe_sources(client, [wanted, other])

        self.assertEqual(client.calls, [("list_sources", 100)])
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["matched_candidate_hashes"], sorted([wanted, other]))
        self.assertFalse(result["provider_mutation_performed"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["private_source_names_persisted"])
        self.assertFalse(result["source_identifiers_persisted"])
        rendered = repr(result)
        self.assertNotIn("Private-Evidence", rendered)
        self.assertNotIn("private-secret-id", rendered)

    def test_repository_fingerprint_is_case_insensitive_and_exact(self) -> None:
        self.assertEqual(
            repository_fingerprint("Example/Repo"),
            repository_fingerprint("example/repo"),
        )
        self.assertNotEqual(
            repository_fingerprint("example/repo"),
            repository_fingerprint("example/repo-2"),
        )

    def test_candidate_hash_validation_is_strict_and_deduplicated(self) -> None:
        digest = repository_fingerprint("example/repo")
        self.assertEqual(parse_candidate_hashes(f"{digest},{digest}"), (digest,))
        with self.assertRaises(ValueError):
            parse_candidate_hashes("not-a-digest")
        with self.assertRaises(ValueError):
            parse_candidate_hashes("")

    def test_probe_module_has_no_provider_mutation_entrypoint(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "ues" / "jules_source_probe.py").read_text(encoding="utf-8")
        for forbidden in ("send_message(", "create_session(", ":sendMessage", "sessions:create"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
