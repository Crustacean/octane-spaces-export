import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests


SPEC = importlib.util.spec_from_file_location("octane_spaces_export", Path(__file__).with_name("main.py"))
exporter = importlib.util.module_from_spec(SPEC)
with patch("requests.Session") as import_session:
    SPEC.loader.exec_module(exporter)
    assert not import_session.called, "Importing the exporter must not contact Octane."


class ExporterTest(unittest.TestCase):
    def test_generated_file_has_shared_url_and_specific_url_on_every_space(self):
        clients = [
            {"client_id": "fixture-client-1", "client_secret": "fixture-secret-1"},
            {"client_id": "fixture-client-2", "client_secret": "fixture-secret-2"},
        ]
        first_session, second_session = Mock(), Mock()
        pages = [
            [{"id": "101", "name": "Shared Space One"}],
            [{"id": "201", "name": "Workspace One"}],
            [
                {"id": "101", "name": "Shared Space One"},
                {"id": "102", "name": "Shared Space Two"},
            ],
            [{"id": "202", "name": "Workspace Two"}],
        ]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"OCTANE_API_CLIENTS": json.dumps(clients)}, clear=True),
            patch.object(exporter, "BASE_URL", "https://octane.example.com"),
            patch.object(exporter, "sign_in", side_effect=[first_session, second_session]) as sign_in,
            patch.object(exporter, "get_all_pages", side_effect=pages) as get_pages,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            output = Path(directory) / "mapping.json"
            self.assertEqual(0, exporter.main(output))
            mapping = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("https://octane.example.com", mapping["shared_url"])
        self.assertEqual(["101", "102"], [s["sharedSpaceId"] for s in mapping["shared_spaces"]])
        for index, space in enumerate(mapping["shared_spaces"]):
            self.assertIn("specific_url", space)
            self.assertEqual("", space["specific_url"])
            self.assertEqual(str(201 + index), space["workspaces"][0]["workspaceId"])
            self.assertEqual(["sharedSpaceId", "sharedSpaceName", "specific_url", "workspaces"], list(space))
        self.assertNotIn("fixture-secret", json.dumps(mapping))
        self.assertEqual(4, get_pages.call_count)
        self.assertEqual(clients, [call.args[0] for call in sign_in.call_args_list])
        first_session.close.assert_called_once()
        second_session.close.assert_called_once()

    def test_single_client_reads_environment_or_prompts_without_hardcoded_credentials(self):
        with (
            patch.dict(os.environ, {"OCTANE_CLIENT_ID": "fixture-client"}, clear=True),
            patch.object(exporter.getpass, "getpass", return_value="fixture-secret") as prompt,
        ):
            self.assertEqual(
                [{"client_id": "fixture-client", "client_secret": "fixture-secret"}],
                exporter.credentials_from_environment(),
            )
            prompt.assert_called_once()

        with (
            patch.dict(
                os.environ,
                {"OCTANE_CLIENT_ID": "fixture-client", "OCTANE_CLIENT_SECRET": "env-secret"},
                clear=True,
            ),
            patch.object(exporter.getpass, "getpass") as prompt,
        ):
            self.assertEqual("env-secret", exporter.credentials_from_environment()[0]["client_secret"])
            prompt.assert_not_called()

    def test_admin_login_takes_precedence(self):
        with patch.dict(
            os.environ,
            {
                "OCTANE_SITE_ADMIN_USER": "fixture-admin",
                "OCTANE_SITE_ADMIN_PASSWORD": "fixture-password",
                "OCTANE_API_CLIENTS": "invalid-json-not-used-for-admin",
            },
            clear=True,
        ):
            self.assertEqual(
                [{"user": "fixture-admin", "password": "fixture-password"}],
                exporter.credentials_from_environment(),
            )

    def test_invalid_client_configuration_is_rejected(self):
        for value in ("[]", "{}", "null", '[{"client_id":"fixture"}]', "invalid-json"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"OCTANE_API_CLIENTS": value}, clear=True
            ):
                with self.assertRaises(ValueError):
                    exporter.credentials_from_environment()

    def test_missing_credentials_do_not_contact_octane_or_write_output(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
            patch.object(exporter, "sign_in") as sign_in,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            output = Path(directory) / "mapping.json"
            self.assertEqual(1, exporter.main(output))
            sign_in.assert_not_called()
            self.assertFalse(output.exists())

    def test_empty_export_preserves_existing_file_and_closes_session(self):
        session = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(exporter, "credentials_from_environment", return_value=[{}]),
            patch.object(exporter, "sign_in", return_value=session),
            patch.object(exporter, "get_all_pages", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            output = Path(directory) / "mapping.json"
            output.write_text("existing mapping", encoding="utf-8")
            self.assertEqual(1, exporter.main(output))
            self.assertEqual("existing mapping", output.read_text(encoding="utf-8"))
        session.close.assert_called_once()

    def test_authentication_failure_does_not_print_response_body(self):
        session = Mock()
        session.post.return_value.status_code = 401
        session.post.return_value.text = "sensitive response body"
        output = io.StringIO()
        with patch.object(exporter.requests, "Session", return_value=session), contextlib.redirect_stdout(output):
            self.assertIsNone(exporter.sign_in({"client_id": "fixture-client"}))
        session.close.assert_called_once()
        self.assertIn("401", output.getvalue())
        self.assertNotIn("sensitive response body", output.getvalue())
        self.assertEqual(exporter.REQUEST_TIMEOUT, session.post.call_args.kwargs["timeout"])

    def test_transport_error_closes_session(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("offline fixture")
        with patch.object(exporter.requests, "Session", return_value=session):
            with self.assertRaises(requests.ConnectionError):
                exporter.sign_in({"client_id": "fixture-client"})
        session.close.assert_called_once()

    def test_pagination_and_timeouts_are_preserved(self):
        session = Mock()
        first, second = Mock(), Mock()
        first.json.return_value = {"data": [{"id": "101"}], "meta": {"total_count": 101}}
        second.json.return_value = {"data": [{"id": "102"}], "meta": {"total_count": 101}}
        session.get.side_effect = [first, second]
        result = exporter.get_all_pages(session, "https://octane.example.com/api/shared_spaces?fields=id,name")
        self.assertEqual([{"id": "101"}, {"id": "102"}], result)
        self.assertTrue(session.get.call_args_list[0].args[0].endswith("&limit=100&offset=0"))
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("&limit=100&offset=100"))
        for call in session.get.call_args_list:
            self.assertEqual(exporter.REQUEST_TIMEOUT, call.kwargs["timeout"])
        first.raise_for_status.assert_called_once()
        second.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
