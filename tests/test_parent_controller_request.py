from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.parent_controller_request import (
    ParentControllerRequestError,
    load_parent_controller_request,
    main,
    validate_parent_controller_request,
)


RUNTIME_SHA = "a" * 40
BASELINE_SHA = "b" * 40


class ParentControllerRequestTests(unittest.TestCase):
    def request(self, project: str = "RP01") -> dict:
        route = "PERSONAL:CEP" if project == "CEP" else project
        return {
            "schema_version": "UES_PARENT_CONTROLLER_REQUEST_V1",
            "request_id": "RP01-U26-0001",
            "project": project,
            "runtime_sha": RUNTIME_SHA,
            "current_authority": {
                "source": "DRIVE_CURRENT_STATE",
                "source_id": "drive-current-state-id",
                "project": project,
                "route": route,
                "current": True,
                "authority_event_id": "RP01-AUTH-20260824-001",
                "expires_at": "2026-08-24T23:30:00+03:00",
                "lineages": {
                    "IPA-S01": {
                        "reviewer": {"provider_starting_branch": "main"},
                    }
                },
                "generation_policy": {
                    "authorized_initial_lineages": {
                        "IPA-S01:REVIEWER": {
                            "authorized": True,
                            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                            "task_spec": {
                                "objective": "Review the frozen page",
                                "exact_baseline": "main@" + BASELINE_SHA,
                                "write_scope": [],
                                "prohibited_scope": ["**"],
                                "validation": ["inspect exact candidate evidence"],
                                "evidence": ["exact candidate findings"],
                                "handoff": "Return structured findings",
                                "stop_gate": "RETURN_FINDINGS",
                            },
                        }
                    }
                },
            },
            "wakeup": {
                "event_type": "EXTERNAL_RECONCILIATION_REQUEST",
                "event_id": "RP01-U26-0001",
                "repository": "hamad933/Bayt-Style",
                "workstream": "IPA-S01",
                "sha": BASELINE_SHA,
            },
        }

    def test_valid_request_is_normalized_and_runtime_bound(self):
        value = validate_parent_controller_request(self.request(), expected_runtime_sha=RUNTIME_SHA)
        self.assertEqual(value["project"], "RP01")
        self.assertEqual(value["runtime_sha"], RUNTIME_SHA)
        self.assertEqual(value["wakeup"]["event_type"], "EXTERNAL_RECONCILIATION_REQUEST")

    def test_all_current_onboarded_projects_are_supported(self):
        for project in ("GS", "CEP", "RP01", "RP02", "RP03", "RP04"):
            with self.subTest(project=project):
                request = self.request(project)
                request["request_id"] = f"{project}-U26-0001"
                request["current_authority"]["authority_event_id"] = f"{project}-AUTH-20260824-001"
                value = validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)
                self.assertEqual(value["project"], project)

    def test_request_is_schema_closed_and_exact_runtime_bound(self):
        request = self.request()
        request["extra"] = "not allowed"
        with self.assertRaises(ParentControllerRequestError):
            validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

        request = self.request()
        request["runtime_sha"] = "c" * 40
        with self.assertRaises(ParentControllerRequestError):
            validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

    def test_drive_current_authority_identity_is_required(self):
        for field, value in (
            ("source", "CHAT"),
            ("current", False),
            ("project", "RP02"),
            ("route", "RP02"),
            ("authority_event_id", ""),
            ("source_id", ""),
            ("expires_at", ""),
        ):
            with self.subTest(field=field):
                request = self.request()
                request["current_authority"][field] = value
                with self.assertRaises(ParentControllerRequestError):
                    validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

    def test_secret_bearing_keys_are_rejected_anywhere(self):
        for key in ("api_key", "token", "password", "secret", "private_key", "client_secret"):
            with self.subTest(key=key):
                request = self.request()
                request["current_authority"]["lineages"]["IPA-S01"]["reviewer"][key] = "do-not-store"
                with self.assertRaises(ParentControllerRequestError):
                    validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

    def test_only_allowlisted_wakeup_type_and_exact_optional_sha_are_accepted(self):
        request = self.request()
        request["wakeup"]["event_type"] = "ARBITRARY_EFFECT"
        with self.assertRaises(ParentControllerRequestError):
            validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

        request = self.request()
        request["wakeup"]["sha"] = "short"
        with self.assertRaises(ParentControllerRequestError):
            validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)

    def test_defaults_keep_request_low_friction(self):
        request = self.request()
        request.pop("wakeup")
        value = validate_parent_controller_request(request, expected_runtime_sha=RUNTIME_SHA)
        self.assertEqual(value["wakeup"]["event_type"], "EXTERNAL_RECONCILIATION_REQUEST")
        self.assertEqual(value["wakeup"]["event_id"], request["request_id"])
        self.assertEqual(value["wakeup"]["repository"], "")

    def test_invalid_json_nonstandard_constants_and_oversized_requests_fail_closed(self):
        with self.assertRaises(ParentControllerRequestError):
            load_parent_controller_request("{not-json", expected_runtime_sha=RUNTIME_SHA)
        with self.assertRaises(ParentControllerRequestError):
            load_parent_controller_request('{"value": NaN}', expected_runtime_sha=RUNTIME_SHA)
        with self.assertRaises(ParentControllerRequestError):
            load_parent_controller_request(" " * (129 * 1024), expected_runtime_sha=RUNTIME_SHA)

    def test_cli_writes_authority_and_sanitized_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "request.json"
            authority_path = root / "authority.json"
            metadata_path = root / "metadata.json"
            input_path.write_text(json.dumps(self.request()), encoding="utf-8")
            rc = main(
                [
                    "--input",
                    str(input_path),
                    "--expected-runtime-sha",
                    RUNTIME_SHA,
                    "--authority-output",
                    str(authority_path),
                    "--metadata-output",
                    str(metadata_path),
                ]
            )
            self.assertEqual(rc, 0)
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(authority["authority_event_id"], "RP01-AUTH-20260824-001")
            self.assertEqual(metadata["project"], "RP01")
            self.assertEqual(metadata["runtime_sha"], RUNTIME_SHA)
            self.assertFalse(metadata["request_file_is_truth_owner"])
            self.assertFalse(metadata["secrets_allowed_in_request"])
            self.assertFalse(metadata["safe_to_blind_retry"])
            self.assertNotIn("current_authority", metadata)


if __name__ == "__main__":
    unittest.main()
